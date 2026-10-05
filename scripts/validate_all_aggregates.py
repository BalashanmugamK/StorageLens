"""
Batch-validate every record in the live DocumentAggregatesTable
through the repository's EXISTING optimizer.

Scan walks all DynamoDB pagination pages. Each record goes through
optimization.adapters.from_aggregate_item
  -> optimization.optimizer.optimize_document
exactly as production would. No optimizer logic is recreated or
modified.

Usage:
    python scripts/validate_all_aggregates.py
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]

Exits non-zero if any record fails.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT))

from load_synthetic_data import resolve_stack_resources

from optimization.adapters import from_aggregate_item
from optimization.optimizer import optimize_document


def scan_all_items(table):
    """Scan every pagination page and return all items."""

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


def classify(result, current_storage_class):
    """NO CHANGE / POSITIVE / NEGATIVE, matching validate_aggregate."""

    if result.recommended_storage_class == current_storage_class:
        return "NO CHANGE"

    if result.savings > 0:
        return "POSITIVE SAVINGS"

    return "NEGATIVE SAVINGS"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Batch-validate all live aggregates through the "
            "repository optimizer."
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

    try:
        resources = resolve_stack_resources(
            args.stack_name,
            args.region,
        )
    except Exception as error:
        print(
            f"ERROR: could not resolve deployed stack "
            f"resources: {error}",
            file=sys.stderr,
        )
        return 4

    aggregates_table_name = resources["aggregates_table"]

    if not aggregates_table_name:
        print(
            "ERROR: DocumentAggregatesTable output not found "
            "in stack.",
            file=sys.stderr,
        )
        return 4

    print(f"Stack:    {args.stack_name}")
    print(f"Region:   {args.region}")
    print(f"Table:    {aggregates_table_name}")
    print("Scanning all aggregate records ...")

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    aggregates_table = dynamodb.Table(aggregates_table_name)

    items = scan_all_items(aggregates_table)

    total = len(items)
    successful = 0
    failed_ids = []
    errors = []

    recommended_counts = Counter()
    verdict_counts = Counter()

    total_current_cost = 0.0
    total_recommended_cost = 0.0
    total_savings = 0.0

    for item in items:
        document_id = item.get("document_id", "<missing-id>")

        try:
            document = from_aggregate_item(item)
            result = optimize_document(document)
        except Exception as error:
            errors.append(
                f"{document_id}: {type(error).__name__}: {error}"
            )
            failed_ids.append(document_id)
            continue

        successful += 1

        recommended_counts[
            result.recommended_storage_class
        ] += 1

        verdict_counts[classify(
            result,
            document.current_storage_class,
        )] += 1

        total_current_cost += result.current_cost
        total_recommended_cost += result.recommended_cost
        total_savings += result.savings

    print()
    print("=" * 62)
    print("BATCH VALIDATION SUMMARY")
    print("(all costs: 12-month projections per optimizer)")
    print("=" * 62)
    print(f"Total records found:      {total}")
    print(f"Successful optimizations: {successful}")
    print(f"Errors:                   {len(errors)}")
    print("=" * 62)

    if failed_ids:
        print("FAILED DOCUMENT IDS:")
        for failed_id in failed_ids:
            print(f"  - {failed_id}")
        print("=" * 62)

    print("RECOMMENDATION COUNTS:")

    for storage_class in sorted(recommended_counts):
        print(
            f"  {storage_class:<28}"
            f"{recommended_counts[storage_class]:>5}"
        )

    print("=" * 62)
    print("VERDICT COUNTS:")

    for verdict in ["NO CHANGE", "POSITIVE SAVINGS", "NEGATIVE SAVINGS"]:
        print(
            f"  {verdict:<28}"
            f"{verdict_counts.get(verdict, 0):>5}"
        )

    print("=" * 62)
    print(f"AGGREGATE CURRENT COST        ${total_current_cost:.6f}")
    print(f"AGGREGATE RECOMMENDED COST    ${total_recommended_cost:.6f}")
    print(f"AGGREGATE SAVINGS             ${total_savings:.6f}")

    if total_current_cost > 0:
        print(
            f"AGGREGATE SAVINGS PERCENTAGE  "
            f"{total_savings / total_current_cost * 100:.2f}%"
        )

    print("=" * 62)

    if errors:
        print(f"ERROR DETAILS ({len(errors)}):")

        for error in errors:
            print(f"  - {error}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())