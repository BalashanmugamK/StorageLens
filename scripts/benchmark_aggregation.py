"""
Benchmark the original sequential aggregation access pattern
against the new bounded-parallel access pattern.

Real data only: documents are read from the deployed
DocumentsTable and access events come from the deployed
AccessHistoryTable through the Lambda's own code
(backend/lambdas/aggregates/app.py). No writes are made
anywhere, so this exercises the exact fetch+compute path and
round-trip latency the Lambda pays.

Sizes benchmarked: ~500 documents and every document currently
in the table (~1000).

Result file:
    experiments/results/aggregation_benchmark.json

Usage:
    python scripts/benchmark_aggregation.py
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT))

# The Lambda module resolves its boto3 resources at import time,
# so the real table names are resolved first (below) and injected
# into the environment before importing it.
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")

RESULTS_JSON = (
    REPO_ROOT / "experiments" / "results"
    / "aggregation_benchmark.json"
)


def scan_all_documents(documents_table):
    documents = []
    scan_kwargs = {}

    while True:
        result = documents_table.scan(**scan_kwargs)

        documents.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return documents


def run_pattern(pattern, documents, cutoff, now):
    """Run one fetch+compute pass and time it."""

    # Imported lazily after main() has pointed the Lambda
    # module's resources at the deployed tables.
    from backend.lambdas.aggregates.app import (
        aggregate_documents_parallel,
        aggregate_documents_sequential,
    )

    runner = (
        aggregate_documents_sequential
        if pattern == "sequential"
        else aggregate_documents_parallel
    )

    started = time.perf_counter()

    aggregates = runner(documents, cutoff, now)

    elapsed = time.perf_counter() - started

    assert len(aggregates) == len(documents)

    events = sum(
        int(aggregate["access_count"])
        for aggregate in aggregates
    )

    return elapsed, events


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark sequential vs bounded-parallel "
            "aggregation against the live tables."
        )
    )

    parser.add_argument(
        "--stack-name",
        default="intelligent-storage-cost-optimizer",
    )

    parser.add_argument(
        "--region",
        default="ap-south-1",
    )

    args = parser.parse_args()

    import boto3

    from load_synthetic_data import resolve_stack_resources

    resources = resolve_stack_resources(
        args.stack_name,
        args.region,
    )

    # Point the Lambda module's own resource handles at the
    # deployed tables, then import its actual code so the
    # benchmark exercises the exact production access pattern.
    os.environ["DOCUMENTS_TABLE"] = (
        resources["documents_table"]
    )
    os.environ["ACCESS_HISTORY_TABLE"] = (
        resources["access_history_table"]
    )
    os.environ["AGGREGATES_TABLE"] = (
        resources["aggregates_table"]
    )

    from backend.lambdas.aggregates.app import (
        aggregate_documents_parallel,
        aggregate_documents_sequential,
    )

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    documents_table = dynamodb.Table(
        resources["documents_table"]
    )

    print(f"Region:         {args.region}")
    print(
        f"DocumentsTable: {resources['documents_table']}"
    )

    documents = scan_all_documents(documents_table)

    print(f"Documents available: {len(documents)}")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    full_count = len(documents)
    subset_count = min(500, full_count)

    sizes = {full_count}
    if subset_count < full_count:
        sizes.add(subset_count)

    runs = []

    for size in sorted(sizes):
        if size == full_count:
            subset = documents
        else:
            # Deterministic subsample: evenly spaced entries.
            step = full_count / size

            subset = [
                documents[int(index * step)]
                for index in range(size)
            ]

        print(f"\n--- {len(subset)} documents ---")

        for pattern in ["sequential", "parallel"]:
            # Per-pattern warm-up before each timed leg so no
            # leg pays one-time costs the others did not pay
            # first (TLS connection establishment through the
            # thread pool, lazy caches on the DynamoDB path).
            run_pattern(
                pattern,
                subset[: min(64, len(subset))],
                cutoff,
                now,
            )

            timings = []

            for _ in range(2):
                elapsed, events = run_pattern(
                    pattern,
                    subset,
                    cutoff,
                    now,
                )

                timings.append(elapsed)

            elapsed = min(timings)

            runs.append(
                {
                    "pattern": pattern,
                    "documents": len(subset),
                    "seconds_pass1": round(timings[0], 3),
                    "seconds_pass2": round(timings[1], 3),
                    "seconds": round(elapsed, 3),
                    "ms_per_document": round(
                        elapsed * 1000 / len(subset),
                        2,
                    ),
                    "aggregated_access_events": events,
                }
            )

            print(
                f"{pattern:<10} {len(subset):>5} docs  "
                f"passes: "
                f"{', '.join(f'{t:.2f}s' for t in timings)}  "
                f"best {elapsed:7.2f}s  "
                f"{elapsed * 1000 / len(subset):7.2f} ms/doc  "
                f"{events} events"
            )

    print()
    print("=" * 62)
    print(
        "BENCHMARK SUMMARY "
        "(live DynamoDB, read-only, Lambda's own code)"
    )
    print("=" * 62)
    print(json.dumps(runs, indent=2))

    RESULTS_JSON.write_text(
        json.dumps(
            {
                "region": args.region,
                "documents_available": full_count,
                "runs": runs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Saved: {RESULTS_JSON}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)