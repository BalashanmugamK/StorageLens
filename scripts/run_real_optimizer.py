"""
Run the EXISTING optimizer against the real-corpus aggregates in
the live DocumentAggregatesTable.

This is the Phase B real-world test's optimizer step. The run
calls optimization.optimizer.optimize_document() on every real
document's aggregate record, with the eligibility policy passed
through (--policy A|B, default B: cost-safe with conflict
reporting).

The stack still contains the Phase A synthetic data by decision,
so this run filters to the REAL documents only, using the
API-issued UUID ids recorded in the dataset's mapping file by
load_real_data.py (default mapping: real_500_mapping.json for
the us_courts dataset; --mapping-file for any other public
legal dataset).

Collected metrics (matching the synthetic experiment's reporting
in audit_all_optimizations.csv):
    documents processed
    optimizer success/errors
    recommended storage-class distribution
    current projected cost
    recommended projected cost
    projected savings / savings percentage
    NO CHANGE / POSITIVE SAVINGS / NEGATIVE SAVINGS counts

Results are written to experiments/results/real_500_results.json
and real_500_results.csv (the synthetic audit CSV is untouched).

Usage:
    python scripts/run_real_optimizer.py
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT))

from load_synthetic_data import resolve_stack_resources

from optimization.adapters import from_aggregate_item
from optimization.constraints import (
    get_eligible_storage_classes,
)
from optimization.optimizer import optimize_document

def resolve_result_paths(mapping_file):
    """
    Result file names follow the mapping file name:
    real_500_mapping.json -> real_500_results.{json,csv};
    any other dataset's mapping file gets its own results pair.
    """

    results_base = (
        REPO_ROOT / "experiments" / "results"
        / mapping_file.name.replace("_mapping", "_results")
    )

    return (
        results_base,
        results_base.with_suffix(".csv"),
    )

# Savings verdicts use the same tolerance as the audit script,
# so the real/synthetic comparison is apples to apples.
SAVINGS_TOLERANCE = 1e-6


def scan_all_items(table):
    items = []
    scan_kwargs = {}

    while True:
        result = table.scan(**scan_kwargs)

        items.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return items


def classify(savings):
    if abs(savings) <= SAVINGS_TOLERANCE:
        return "NO CHANGE"

    if savings > 0:
        return "POSITIVE SAVINGS"

    return "NEGATIVE SAVINGS"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run the existing optimizer over the real "
            "document aggregates."
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

    parser.add_argument(
        "--mapping-file",
        default=None,
        help=(
            "Mapping file under experiments/results/. Defaults "
            "to real_500_mapping.json (us_courts dataset); pass "
            "the file name for any other public legal dataset."
        ),
    )

    parser.add_argument(
        "--policy",
        choices=["A", "B"],
        default="B",
        help=(
            "Eligibility policy: A = state-constrained "
            "(may recommend cost-increasing migrations), "
            "B = cost-safe (current class joins the candidate "
            "set, conflicts reported). Default: B."
        ),
    )

    args = parser.parse_args()

    import boto3

    from boto3.dynamodb.conditions import Key as DynamoKey

    if args.mapping_file:
        mapping_file = (
            REPO_ROOT / "experiments" / "results"
            / args.mapping_file
        )
    else:
        mapping_file = (
            REPO_ROOT / "experiments" / "results"
            / "real_500_mapping.json"
        )

    RESULTS_JSON, RESULTS_CSV = resolve_result_paths(mapping_file)

    mapping = json.loads(
        mapping_file.read_text(encoding="utf-8")
    )

    real_by_id = {
        entry["api_document_id"]: entry
        for entry in mapping["documents"]
    }

    real_id_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    resources = resolve_stack_resources(
        args.stack_name,
        args.region,
    )

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    aggregates_table = dynamodb.Table(
        resources["aggregates_table"]
    )

    access_history_table = dynamodb.Table(
        resources["access_history_table"]
    )

    print(f"Stack:    {args.stack_name}")
    print(f"Region:   {args.region}")
    print(f"Table:    {resources['aggregates_table']}")
    print("Scanning all aggregate records ...")

    items = scan_all_items(aggregates_table)

    real_items = [
        item
        for item in items
        if item["document_id"] in real_by_id
    ]

    print(
        f"Aggregates in table: {len(items)}; "
        f"real-corpus aggregates: {len(real_items)}"
    )

    if len(real_items) != len(mapping["documents"]):
        print(
            f"WARNING: expected {len(mapping['documents'])} "
            f"real aggregates, found {len(real_items)}.",
            file=sys.stderr,
        )

    # Count the real documents' access events directly from the
    # access-history table, per real document id.
    total_events = 0

    for document_id in real_by_id:
        result = access_history_table.query(
            KeyConditionExpression=(
                DynamoKey("document_id").eq(document_id)
            ),
            Select="COUNT",
        )
        total_events += result.get("Count", 0)

    rows = []
    errors = []

    recommended_counts = Counter()
    verdict_counts = Counter()
    eligible_state_counts = Counter()
    conflict_count = 0

    current_total = 0.0
    recommended_total = 0.0

    for item in sorted(
        real_items,
        key=lambda record: record["document_id"],
    ):
        document_id = item["document_id"]
        manifest_entry = real_by_id[document_id]

        try:
            document = from_aggregate_item(item)
            result = optimize_document(
                document,
                policy=args.policy,
            )
        except Exception as error:
            errors.append(
                {
                    "document_id": document_id,
                    "error": (
                        f"{type(error).__name__}: {error}"
                    ),
                }
            )
            continue

        verdict = classify(result.savings)

        verdict_counts[verdict] += 1

        if getattr(result, "policy_conflict", False):
            conflict_count += 1

        recommended_counts[
            result.recommended_storage_class
        ] += 1
        eligible_state_counts[str(
            document.document_state
        )] += 1

        current_total += result.current_cost
        recommended_total += result.recommended_cost

        tier_costs = {
            tier.storage_class: tier.cost.total_cost
            for tier in result.tier_costs
        }

        rows.append(
            {
                "manifest_document_id": manifest_entry[
                    "manifest_document_id"
                ],
                "granule_id": manifest_entry.get(
                    "granule_id",
                    manifest_entry["manifest_document_id"],
                ),
                "document_id": document.document_id,
                "document_state": (
                    document.document_state or ""
                ),
                "court": manifest_entry.get("court", ""),
                "date_issued": manifest_entry.get(
                    "date_issued", ""
                ),
                "file_size_bytes": (
                    document.file_size_bytes
                ),
                "current_storage_class": (
                    document.current_storage_class
                ),
                "access_count": document.access_count,
                "access_frequency": (
                    f"{document.access_frequency:.12f}"
                ),
                "days_since_last_access": (
                    document.days_since_last_access
                    if document.days_since_last_access
                    is not None
                    else ""
                ),
                "eligible_classes": "|".join(
                    get_eligible_storage_classes(
                        document.document_state
                    )
                ),
                "recommended_storage_class": (
                    result.recommended_storage_class
                ),
                "current_annual_cost": (
                    f"{result.current_cost:.10f}"
                ),
                "recommended_annual_cost": (
                    f"{result.recommended_cost:.10f}"
                ),
                "savings": f"{result.savings:.10f}",
                "savings_percentage": (
                    f"{result.savings_percentage:.4f}"
                ),
                "verdict": verdict,
                "policy": (result.policy),
                "policy_conflict": (
                    "YES" if result.policy_conflict else "NO"
                ),
                "tier_costs": " | ".join(
                    f"{storage_class}"
                    f"={cost:.10f}"
                    for storage_class, cost in sorted(
                        tier_costs.items()
                    )
                ),
            }
        )

    documents_count = len(rows)
    success_count = documents_count
    error_count = len(errors)

    projected_savings = current_total - recommended_total
    savings_percentage = (
        (projected_savings / current_total) * 100
        if current_total > 0
        else 0.0
    )

    results = {
        "dataset": "real",
        "source": mapping.get(
            "source_dataset",
            "data/real/us_courts/manifest.json",
        ),
        "policy": args.policy,
        "policy_conflict_count": conflict_count,
        "stack": args.stack_name,
        "region": args.region,
        "aggregates_table": resources["aggregates_table"],
        "aggregates_in_table": len(items),
        "documents_processed": documents_count,
        "access_events_on_record": total_events,
        "optimizer_success": success_count,
        "optimizer_errors": error_count,
        "errors": errors,
        "document_state_counts": dict(
            eligible_state_counts
        ),
        "current_storage_class_counts": dict(
            Counter(
                row["current_storage_class"]
                for row in rows
            )
        ),
        "recommended_storage_class_counts": dict(
            recommended_counts
        ),
        "current_projected_annual_cost": (
            f"{current_total:.10f}"
        ),
        "recommended_projected_annual_cost": (
            f"{recommended_total:.10f}"
        ),
        "projected_savings": f"{projected_savings:.10f}",
        "savings_percentage": (
            f"{savings_percentage:.4f}"
        ),
        "no_change_count": verdict_counts.get(
            "NO CHANGE", 0
        ),
        "positive_savings_count": verdict_counts.get(
            "POSITIVE SAVINGS", 0
        ),
        "negative_savings_count": verdict_counts.get(
            "NEGATIVE SAVINGS", 0
        ),
    }

    results_json = dict(results)
    results_json["results"] = rows

    RESULTS_JSON.write_text(
        json.dumps(results_json, indent=2),
        encoding="utf-8",
    )

    if rows:
        fieldnames = list(rows[0].keys())

        with RESULTS_CSV.open(
            "w", newline="", encoding="utf-8"
        ) as file:
            writer = csv.DictWriter(
                file, fieldnames=fieldnames
            )
            writer.writeheader()
            writer.writerows(rows)

    print()
    print("=" * 62)
    print("REAL-DATA OPTIMIZER RUN SUMMARY")
    print("=" * 62)
    print(f"Real documents:           {documents_count}")
    print(f"Eligibility policy:       {args.policy}")
    print(f"Policy conflicts:         {conflict_count}")
    print(f"Access events:            {total_events}")
    print(f"Optimizer success:        {success_count}")
    print(f"Optimizer errors:         {error_count}")
    print(
        f"Current projected cost:   "
        f"${current_total:.8f}"
    )
    print(
        f"Recommended projected:    "
        f"${recommended_total:.8f}"
    )
    print(
        f"Projected savings:        "
        f"${projected_savings:.8f} "
        f"({savings_percentage:.4f}%)"
    )
    print("RECOMMENDED TIER DISTRIBUTION:")

    for storage_class in sorted(recommended_counts):
        print(
            f"  {storage_class:<28}"
            f"{recommended_counts[storage_class]:>5}"
        )

    print("SAVINGS VERDICTS:")

    for verdict in [
        "POSITIVE SAVINGS",
        "NO CHANGE",
        "NEGATIVE SAVINGS",
    ]:
        print(
            f"  {verdict:<28}"
            f"{verdict_counts.get(verdict, 0):>5}"
        )

    print("=" * 62)
    print(f"Results JSON: {RESULTS_JSON}")
    print(f"Results CSV:  {RESULTS_CSV}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())