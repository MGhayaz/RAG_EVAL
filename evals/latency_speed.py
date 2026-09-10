from __future__ import annotations
import sys
from pathlib import Path
import time
sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))
from retriver import Retriever,MODEL_NAME




TEST_QUERY = (
    "What are the top 3 most expensive products that are currently in stock, "
    "excluding products from the Electronics category, and include the product "
    "name, category, price, and stock quantity?"
)

RUNS = 5


def main() -> None:
    retriever = Retriever()
    timings: list[float] = []

    print(f"Running Model: {MODEL_NAME}, for {RUNS} API calls...\n")

    for i in range(1, RUNS + 1):
        start = time.perf_counter()
        result = retriever.retrieve(TEST_QUERY)
        elapsed = time.perf_counter() - start
        timings.append(elapsed)

        print(f"Run {i}: {elapsed:.3f}s")
        print(f"SQL: {result['sql']}\n")

    average = sum(timings) / len(timings)

    print("=" * 40)
    print(f"Average time: {average:.3f}s")
    print(f"Min time:     {min(timings):.3f}s")
    print(f"Max time:     {max(timings):.3f}s")
    print("=" * 40)


if __name__ == "__main__":
    main()
