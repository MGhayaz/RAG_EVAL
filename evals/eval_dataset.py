"""
Text-to-SQL Eval Harness
------------------------
Runs each question through the retriever, executes both gold_sql and
predicted_sql against a read-only MySQL connection, compares result rows,
and produces a structured JSON report.

Usage:
    python eval_harness.py

Requires:
    pip install mysql-connector-python --break-system-packages
"""

import json
import time
import sys
from pathlib import Path
from decimal import Decimal
from datetime import date, datetime
from dataclasses import dataclass, field, asdict
from typing import Any
from tabulate import tabulate
from sqlalchemy import create_engine, text

sys.path.append(str(Path(__file__).resolve().parents[1] / "src")) 
from config import settings
from retriver import Retriever

BASE_DIR = Path(__file__).resolve().parent
DATASET_PATH = BASE_DIR / "dataset.json"
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
 
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
REPORT_PATH = REPORTS_DIR / f"eval_report_{RUN_TIMESTAMP}.json"
LATEST_REPORT_PATH = REPORTS_DIR / "eval_report_latest.json"
HISTORY_PATH = REPORTS_DIR / "history.jsonl"   # one line per run, for trend tracking
 
# ── CONFIG ────────────────────────────────────────────────────────────────
FLOAT_TOLERANCE_DECIMALS = 2
RATE_LIMIT_DELAY_SECONDS = 4.5     # spacing between calls — free-tier friendly
MAX_RETRIES_ON_INFRA_FAILURE = 3
RETRY_BASE_DELAY_SECONDS = 6.0     # exponential backoff base
 
engine = create_engine(settings.DB_URL, pool_pre_ping=True)
_retriever = Retriever()  # instantiated once, reused across all questions


# ── DATA MODEL ────────────────────────────────────────────────────────────
@dataclass
class QuestionResult:
    id: str
    question: str
    category: list
    expected_behavior: str
    gold_sql: str
    predicted_sql: str
    status: str                # PASS | FAIL | ERROR | SKIPPED
    failure_type: str = ""     # SQL_SYNTAX_ERROR | EMPTY_RESULT | WRONG_ROWS |
                                # PARTIAL_MATCH | INVALID_REQUEST_MISMATCH |
                                # FALSE_REFUSAL | RETRIEVER_EXCEPTION |
                                # UNSAFE_OR_MALFORMED_SQL | GOLD_SQL_BROKEN | ""
    gold_row_count: int = 0
    predicted_row_count: int = 0
    diff_sample: dict = field(default_factory=dict)   # small preview, not full dump
    runtime_seconds: float = 0.0
    notes: str = ""


# ── NORMALIZATION HELPERS ────────────────────────────────────────────────
def normalize_value(v: Any) -> Any: # ye function ka kaam hai output me se aaye row ki value me agar decimals/floats wagera jo gold query values se compare karne dikkat karsakte, unku round off karne ke liye hai
    """Make a single cell value comparison-safe."""
    if isinstance(v, Decimal):
        return round(float(v), FLOAT_TOLERANCE_DECIMALS)
    if isinstance(v, float):
        return round(v, FLOAT_TOLERANCE_DECIMALS)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    # if v is None:
    #     return None
    # return v


def normalize_row(row) -> tuple:
    """
    Normalize a row for comparison. Accepts either a tuple/Row (from our
    own gold_sql execution) or a dict (from Retriever.retrieve(), which
    returns rows via .mappings().all()).
    Values are sorted WITHIN the row so column-order/column-name differences
    between gold_sql and predicted_sql don't cause false negatives.
    Trade-off: assumes no two columns in the same row share overlapping
    value spaces in a way that could coincidentally match. True for this
    schema (names/emails/dates/amounts are all distinct types/ranges).
    """
    values = row.values() if isinstance(row, dict) else row
    normalized = [normalize_value(v) for v in values]
    return tuple(sorted(normalized, key=lambda x: str(x)))


def normalize_result_set(rows: list) -> set:
    return {normalize_row(row) for row in rows}


# ── SQL EXECUTION ─────────────────────────────────────────────────────────
def is_safe_select(sql: str) -> bool:
    """Defense in depth: only allow single SELECT statements even though
    the system prompt should already guarantee this."""
    cleaned = sql.strip().rstrip(";") # query string se spaces hatadeta if any present then last me present ; ku bi hatadeta 
    if ";" in cleaned:
        return False
    return cleaned.lower().startswith("select")


def execute_query(conn, sql: str) -> tuple[list, str]:
    """
    Runs gold_sql only (predicted SQL is already executed inside
    Retriever.retrieve()). Returns (rows, error_message); error_message
    is "" on success. Rows come back as list[dict] via .mappings() so
    they're directly comparable with retriever's predicted rows.
    """
    if not is_safe_select(sql):
        return [], "REJECTED_NON_SELECT_OR_MULTI_STATEMENT"
    try:
        result = conn.execute(text(sql))
        rows = [dict(row) for row in result.mappings().all()]
        return rows, ""
    except Exception as exc:
        conn.rollback()
        return [], f"SQL_ERROR: {exc}"


# ── YOUR RETRIEVER ─────────────────────────────────────────────────────
# retrieve() already executes the SQL internally and returns a dict:
#   {"question": ..., "sql": str|None, "rows": list[dict], "status": str}
# status is one of: "success" | "invalid_request" | "service_unavailable"
def call_retriever_with_retry(question: str) -> dict:
    """
    Paces requests to respect free-tier rate limits, and retries with
    exponential backoff specifically when the retriever reports
    'service_unavailable' (infra issue, not a model-quality failure).
    """
    last_result = None
    for attempt in range(MAX_RETRIES_ON_INFRA_FAILURE):
        result = _retriever.retrieve(question)
        last_result = result
        if result["status"] != "service_unavailable":
            time.sleep(RATE_LIMIT_DELAY_SECONDS)  # pace even on success
            return result
        wait = RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
        time.sleep(wait)
    return last_result  # still service_unavailable after all retries


# ── CORE EVAL LOOP ────────────────────────────────────────────────────────
def evaluate_question(conn, q: dict) -> QuestionResult:
    start = time.time()

    try:
        retrieval = call_retriever_with_retry(q["question"])
    except Exception as exc:
        # retrieve() raises ValueError for non-SELECT / multi-statement SQL.
        # Don't let one bad generation kill the whole eval run — classify it.
        runtime = time.time() - start
        return QuestionResult(
            id=q["id"], question=q["question"], category=q.get("category", []),
            expected_behavior=q["expected_behavior"], gold_sql=q["gold_sql"],
            predicted_sql="", status="FAIL", failure_type="RETRIEVER_EXCEPTION",
            runtime_seconds=round(runtime, 3),
            notes=f"{q.get('notes', '')} | Retriever raised: {exc}",
        )

    runtime = time.time() - start
    predicted_sql = retrieval.get("sql") or ""

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

    # Infra failure — not a model-quality signal, don't count it as pass/fail
    if retrieval["status"] == "service_unavailable":
        result.status = "SKIPPED"
        result.failure_type = "SERVICE_UNAVAILABLE"
        result.notes += " | Gemini API unavailable — retry this question separately."
        return result

    # Case 1: this question expects the model to refuse
    if q["expected_behavior"] == "invalid_request":
        if retrieval["status"] == "invalid_request":
            result.status = "PASS"
        else:
            result.status = "FAIL"
            result.failure_type = "INVALID_REQUEST_MISMATCH"
            result.notes += " | Expected refusal, model attempted a query."
        return result

    # Case 2: valid_request — model should NOT refuse
    if retrieval["status"] == "invalid_request":
        result.status = "FAIL"
        result.failure_type = "FALSE_REFUSAL"
        result.notes += " | Model refused a satisfiable question."
        return result

    # Execute gold (predicted was already executed inside retrieve())
    gold_rows, gold_err = execute_query(conn, q["gold_sql"])
    if gold_err:
        # gold_sql itself broken — this is a dataset bug, flag loudly
        result.status = "ERROR"
        result.failure_type = "GOLD_SQL_BROKEN"
        result.notes += f" | GOLD SQL FAILED: {gold_err}"
        return result

    pred_rows = retrieval["rows"]  # already list[dict] from retrieve()
    result.gold_row_count = len(gold_rows)
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
    # keep the diff small — first 3 rows only-in-gold / only-in-predicted
    result.diff_sample = {
        "only_in_gold": list(gold_set - pred_set)[:3],
        "only_in_predicted": list(pred_set - gold_set)[:3],
    }
    return result

def build_report(results: list[QuestionResult]) -> dict:
    skipped = [r for r in results if r.status == "SKIPPED"]
    scored = [r for r in results if r.status != "SKIPPED"]
 
    total = len(scored)
    passed = sum(1 for r in scored if r.status == "PASS")
 
    category_stats = {}
    for r in scored:
        for cat in (r.category or ["uncategorized"]):
            category_stats.setdefault(cat, {"total": 0, "passed": 0})
            category_stats[cat]["total"] += 1
            if r.status == "PASS":
                category_stats[cat]["passed"] += 1
 
    failure_type_counts = {}
    for r in scored:
        if r.status != "PASS" and r.failure_type:
            failure_type_counts[r.failure_type] = failure_type_counts.get(r.failure_type, 0) + 1
 
    return {
        "run_timestamp": RUN_TIMESTAMP,
        "summary": {
            "total_questions": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy_pct": round(100 * passed / total, 2) if total else 0,
            "skipped_due_to_infra": len(skipped),
            "skipped_ids": [r.id for r in skipped],
        },
        "category_breakdown": {
            cat: {**stats, "accuracy_pct": round(100 * stats["passed"] / stats["total"], 2)}
            for cat, stats in category_stats.items()
        },
        "failure_type_counts": failure_type_counts,
        "results": [asdict(r) for r in results],
    }
 
 
def print_report(report: dict) -> None:
    s = report["summary"]
    print("\n" + "=" * 64)
    print(f" EVAL RUN {report['run_timestamp']}  —  {s['passed']}/{s['total_questions']} passed ({s['accuracy_pct']}%)")
    if s["skipped_due_to_infra"]:
        print(f" Skipped (infra/rate-limit): {s['skipped_due_to_infra']} — {s['skipped_ids']}")
    print("=" * 64)
 
    print("\n-- Category Breakdown --")
    cat_rows = sorted(
        [[cat, f"{v['passed']}/{v['total']}", f"{v['accuracy_pct']}%"]
         for cat, v in report["category_breakdown"].items()],
        key=lambda row: row[0]
    )
    print(tabulate(cat_rows, headers=["Category", "Passed", "Accuracy"], tablefmt="grid"))
 
    if report["failure_type_counts"]:
        print("\n-- Failure Type Counts --")
        ft_rows = sorted(report["failure_type_counts"].items(), key=lambda x: -x[1])
        print(tabulate(ft_rows, headers=["Failure Type", "Count"], tablefmt="grid"))
 
    results = report["results"]
    passed_qs = [r for r in results if r["status"] == "PASS"]
    failed_qs = [r for r in results if r["status"] == "FAIL"]
    error_qs = [r for r in results if r["status"] == "ERROR"]
    skipped_qs = [r for r in results if r["status"] == "SKIPPED"]
 
    print(f"\n-- PASSED ({len(passed_qs)}) --")
    if passed_qs:
        print(tabulate(
            [[r["id"], r["question"][:55]] for r in passed_qs],
            headers=["ID", "Question"], tablefmt="simple"
        ))
 
    print(f"\n-- FAILED ({len(failed_qs)}) --")
    if failed_qs:
        print(tabulate(
            [[r["id"], r["question"][:40], ",".join(r["category"]), r["failure_type"]] for r in failed_qs],
            headers=["ID", "Question", "Category", "Failure Type"], tablefmt="grid"
        ))
    else:
        print("None.")
 
    if error_qs:
        print(f"\n-- ERRORS ({len(error_qs)}) — dataset/gold_sql problems, not model failures --")
        print(tabulate(
            [[r["id"], r["failure_type"], r["notes"][:55]] for r in error_qs],
            headers=["ID", "Type", "Notes"], tablefmt="grid"
        ))
 
    if skipped_qs:
        print(f"\n-- SKIPPED ({len(skipped_qs)}) — infra/rate-limit, re-run separately --")
        print(tabulate([[r["id"], r["question"][:55]] for r in skipped_qs],
                        headers=["ID", "Question"], tablefmt="simple"))
    print()
 
 
def append_history(report: dict) -> None:
    """One line per run — lets you track accuracy trend over time without
    opening every individual timestamped report."""
    entry = {"timestamp": report["run_timestamp"], **report["summary"]}
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def run_eval():
    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    with engine.connect() as conn:
        results = [evaluate_question(conn, q) for q in dataset]

    skipped = [r for r in results if r.status == "SKIPPED"]
    scored = [r for r in results if r.status != "SKIPPED"]  # infra failures excluded from accuracy

    total = len(scored)
    passed = sum(1 for r in scored if r.status == "PASS")

    # per-category breakdown (scored questions only)
    category_stats = {}
    for r in scored:
        for cat in (r.category or ["uncategorized"]):
            category_stats.setdefault(cat, {"total": 0, "passed": 0})
            category_stats[cat]["total"] += 1
            if r.status == "PASS":
                category_stats[cat]["passed"] += 1

    failure_type_counts = {}
    for r in scored:
        if r.status != "PASS" and r.failure_type:
            failure_type_counts[r.failure_type] = failure_type_counts.get(r.failure_type, 0) + 1

    report = {
        "summary": {
            "total_questions": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy_pct": round(100 * passed / total, 2) if total else 0,
            "skipped_due_to_infra": len(skipped),
            "skipped_ids": [r.id for r in skipped],
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