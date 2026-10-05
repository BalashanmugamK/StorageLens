"""
Validate one live aggregate through the repository's EXISTING
optimizer.

Fetches the DocumentAggregatesTable item for a document from the
deployed stack, bridges it with
optimization.adapters.from_aggregate_item, and runs
optimization.optimizer.optimize_document - no optimizer logic is
recreated or modified.

Usage:
    python scripts/validate_aggregate.py --document-id synthetic-0348
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]

Exit codes:
    0  success
    2  document not found in DocumentAggregatesTable
    3  optimizer itself errored
    4  stack/infrastructure lookup failed
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Reuse the stack-output resolution from the load script
# (bucket/table names always come from CloudFormation outputs).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT))

from load_synthetic_data import resolve_stack_resources

from optimization.adapters import from_aggregate_item
from optimization.optimizer import optimize_document
from optimization.outputs import optimization_result_to_dict


def as_float(value):
    """Convert DynamoDB Decimal values for display."""

    return None if value is None else float(value)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Validate a live aggregate item through the "
            "repository optimizer."
        )
    )

    parser.add_argument(
        "--document-id",
        required=True,
        help="document_id of the aggregate record to validate",
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
            "ERROR: DocumentAggregatesTable output not "
            "found in stack.",
            file=sys.stderr,
        )
        return 4

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    aggregates_table = dynamodb.Table(aggregates_table_name)

    result = aggregates_table.get_item(
        Key={"document_id": args.document_id},
        ConsistentRead=True,
    )

    item = result.get("Item")

    if not item:
        print(
            f"ERROR: no aggregate record found for "
            f"document_id={args.document_id} in "
            f"{aggregates_table_name}.",
            file=sys.stderr,
        )
        return 2

    # Run the item through the repository's existing bridge
    # and optimizer only - identical to production data flow.
    try:
        document = from_aggregate_item(item)
        optimization = optimize_document(document)
    except Exception as error:
        print(
            f"ERROR: optimizer failed for "
            f"{args.document_id}: {error}",
            file=sys.stderr,
        )
        return 3

    print("=" * 62)
    print(f"DOCUMENT              {document.document_id}")
    print(f"document_state        {document.document_state}")
    print(f"file_size_bytes       {document.file_size_bytes}")
    print(
        f"current_storage_class "
        f"{document.current_storage_class}"
    )
    print(f"access_count          {document.access_count}")
    print(
        f"access_frequency      "
        f"{as_float(item['access_frequency'])}"
    )
    print(
        f"days_since_last_access "
        f"{as_float(item['days_since_last_access'])}"
    )
    print("=" * 62)
    print(f"RECOMMENDED CLASS     {optimization.recommended_storage_class}")

    if (
        optimization.eligible_storage_classes
        is not None
    ):
        print(
            f"ELIGIBLE CLASSES      "
            f"{', '.join(optimization.eligible_storage_classes)}"
        )

    print("-" * 62)
    print(f"CURRENT ANNUAL COST       ${optimization.current_cost:.6f}")
    print(
        f"RECOMMENDED ANNUAL COST   "
        f"${optimization.recommended_cost:.6f}"
    )
    print(f"SAVINGS                   ${optimization.savings:.6f}")
    print(
        f"SAVINGS PERCENTAGE        "
        f"{optimization.savings_percentage:.2f}%"
    )
    print("-" * 62)

    # Classify the recommendation outcome.
    already_optimal = (
        optimization.recommended_storage_class
        == document.current_storage_class
    )

    if already_optimal:
        verdict = "NO CHANGE - current tier is already optimal"
    elif optimization.savings > 0:
        verdict = (
            "POSITIVE SAVINGS - transition is "
            "cost-reducing and eligible"
        )
    else:
        verdict = (
            "NEGATIVE SAVINGS - every eligible class costs "
            "more than the current tier; the eligibility rule "
            "drives the recommendation, not cost"
            if optimization.savings < 0
            and document.current_storage_class
            not in optimization.eligible_storage_classes
            else (
                "NEGATIVE SAVINGS - recommended tier is "
                "eligible but costs more than the current one"
            )
        )

    print(f"VERDICT: {verdict}")

    print("\nFull optimizer result (dashboard JSON):")
    print(
        json.dumps(
            optimization_result_to_dict(optimization),
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())