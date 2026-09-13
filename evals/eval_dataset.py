"""
Text-to-SQL Eval Harness
------------------------
Runs each question through the retriever, executes both gold_sql and
predicted_sql against a read-only MySQL connection, compares result rows,
and produces a structured JSON report.
"""

import json
import time
from decimal import Decimal
from datetime import date, datetime
from dataclasses import dataclass, field, asdict
from typing import Any

from retriver import Retriever
from config import settings

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

engine = create_engine(settings.DB_URL)

FLOAT_TOLERANCE_DECIMALS = 2  # round DECIMAL/float values to this many places
DATASET_PATH = "dataset.json"          # your 18-question dataset
REPORT_PATH = "eval_report.json"


# ── DATA MODEL ────────────────────────────────────────────────────────────
@dataclass
class QuestionResult:
    id: str
    question: str
    category: list
    expected_behavior: str
    gold_sql: str
    predicted_sql: str
    status: str                # PASS | FAIL | ERROR
    failure_type: str = ""     # SQL_SYNTAX_ERROR | EMPTY_RESULT | WRONG_ROWS |
                                # PARTIAL_MATCH | INVALID_REQUEST_MISMATCH | ""
    gold_row_count: int = 0
    predicted_row_count: int = 0
    diff_sample: dict = field(default_factory=dict)   # small preview, not full dump
    runtime_seconds: float = 0.0
    notes: str = ""


# ── NORMALIZATION HELPERS ────────────────────────────────────────────────
def normalize_value(v: Any) -> Any:
    """Make a single cell value comparison-safe."""
    if isinstance(v, Decimal):
        return round(float(v), FLOAT_TOLERANCE_DECIMALS)
    if isinstance(v, float):
        return round(v, FLOAT_TOLERANCE_DECIMALS)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if v is None:
        return None
    return v


def normalize_row(row: tuple) -> tuple:
    """
    Normalize a row for comparison.
    Values are sorted WITHIN the row so column-order differences between
    gold_sql and predicted_sql don't cause false negatives.
    Trade-off: assumes no two columns in the same row share overlapping
    value spaces in a way that could coincidentally match. True for this
    schema (names/emails/dates/amounts are all distinct types/ranges).
    """
    normalized = [normalize_value(v) for v in row]
    # sort by string repr to keep it stable across mixed types
    return tuple(sorted(normalized, key=lambda x: str(x)))


def normalize_result_set(rows: list) -> set:
    return {normalize_row(r) for r in rows}


# ── SQL EXECUTION ─────────────────────────────────────────────────────────
def is_safe_select(sql: str) -> bool:
    """Defense in depth: only allow single SELECT statements even though
    the system prompt should already guarantee this."""
    cleaned = sql.strip().rstrip(";")
    if ";" in cleaned:
        return False
    return cleaned.lower().startswith("select")


def execute_query(
    conn: Connection,
    sql: str,
) -> tuple[list[tuple], str | None]:
    try:
        result = conn.execute(text(sql))
        rows = result.fetchall()
        return rows, None
    except Exception as exc:
        return [], str(exc)


# ── YOUR RETRIEVER — replace this stub with your real import ─────────────
def call_retriever(question: str) -> str:
        r = Retriever()
        return r.retrieve(question)
    


# ── CORE EVAL LOOP ────────────────────────────────────────────────────────
def evaluate_question(conn: Connection, q: dict) -> QuestionResult:
    start = time.time()

    predicted_sql = call_retriever(q["question"]).strip()
    runtime = time.time() - start

    result = QuestionResult(
        id=q["id"],
        question=q["question"],
        category=q.get("category", []),
        expected_behavior=q["expected_behavior"],
        gold_sql=q["gold_sql"],
        predicted_sql=predicted_sql,
        status="ERROR",
        runtime_seconds=round(runtime, 3),
        notes=q.get("notes", ""),
    )

    # Case 1: question expects the model to refuse
    if q["expected_behavior"] == "invalid_request":
        if predicted_sql == "INVALID_REQUEST":
            result.status = "PASS"
        else:
            result.status = "FAIL"
            result.failure_type = "INVALID_REQUEST_MISMATCH"
            result.notes += (
                " | Expected refusal, model attempted a query."
            )
        return result

    # Case 2: valid request — model should NOT refuse
    if predicted_sql == "INVALID_REQUEST":
        result.status = "FAIL"
        result.failure_type = "FALSE_REFUSAL"
        result.notes += (
            " | Model refused a satisfiable question."
        )
        return result

    # Execute gold SQL
    gold_rows, gold_err = execute_query(conn, q["gold_sql"])

    if gold_err:
        result.status = "ERROR"
        result.failure_type = "GOLD_SQL_BROKEN"
        result.notes += f" | GOLD SQL FAILED: {gold_err}"
        return result

    # Execute predicted SQL
    pred_rows, pred_err = execute_query(conn, predicted_sql)

    result.gold_row_count = len(gold_rows)

    if pred_err:
        result.status = "FAIL"
        result.failure_type = "SQL_SYNTAX_ERROR"
        result.notes += f" | {pred_err}"
        return result

    result.predicted_row_count = len(pred_rows)

    gold_set = normalize_result_set(gold_rows)
    pred_set = normalize_result_set(pred_rows)

    if gold_set == pred_set:
        result.status = "PASS"
        return result

    # Failure — classify further
    if len(pred_rows) == 0 and len(gold_rows) > 0:
        result.failure_type = "EMPTY_RESULT"
    elif gold_set & pred_set:
        result.failure_type = "PARTIAL_MATCH"
    else:
        result.failure_type = "WRONG_ROWS"

    result.status = "FAIL"

    result.diff_sample = {
        "only_in_gold": list(gold_set - pred_set)[:3],
        "only_in_predicted": list(pred_set - gold_set)[:3],
    }

    return result


def run_eval() -> None:
    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    with engine.connect() as conn:
        results = [
            evaluate_question(conn, q)
            for q in dataset
        ]

    total = len(results)
    passed = sum(
        1 for r in results
        if r.status == "PASS"
    )

    # per-category breakdown
    category_stats = {}
    for r in results:
        for cat in (r.category or ["uncategorized"]):
            category_stats.setdefault(cat, {"total": 0, "passed": 0})
            category_stats[cat]["total"] += 1
            if r.status == "PASS":
                category_stats[cat]["passed"] += 1

    failure_type_counts = {}
    for r in results:
        if r.status != "PASS" and r.failure_type:
            failure_type_counts[r.failure_type] = failure_type_counts.get(r.failure_type, 0) + 1

    report = {
        "summary": {
            "total_questions": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy_pct": round(100 * passed / total, 2) if total else 0,
        },
        "category_breakdown": {
            cat: {
                **stats,
                "accuracy_pct": round(100 * stats["passed"] / stats["total"], 2),
            }
            for cat, stats in category_stats.items()
        },
        "failure_type_counts": failure_type_counts,
        "results": [asdict(r) for r in results],
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"Accuracy: {passed}/{total} ({report['summary']['accuracy_pct']}%)")
    print(f"Report written to {REPORT_PATH}")


if __name__ == "__main__":
    run_eval()