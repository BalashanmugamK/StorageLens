"""
Snapshot DocumentAggregatesTable items so the existing experiments
are not invalidated by a later full-table AggregatesFunction run.

Running the live POST /aggregates/run re-aggregates EVERY document
in the stack (the function has no per-dataset filter). That is fine
for the diversified documents, but it recomputes the aggregate rows
backing the existing D.C. real experiment and the synthetic
experiment (their access events stay in AccessHistoryTable; only
aggregate values like days_since_last_access drift by the time
elapsed between runs).

Snapshot mode stores the pre-run aggregate items locally.
Restore mode writes the snapshot rows BACK for documents that are
NOT part of the diversified corpus mapping ids (the diversified
rows keep their own freshly-verified aggregates).

Usage:
    python scripts/snapshot_aggregates.py snapshot \
        --stack-name intelligent-storage-cost-optimizer \
        --region ap-south-1
    python scripts/snapshot_aggregates.py restore \
        --exclude-mapping experiments/results/diversified_500_mapping.json \
        [--report]

The restore is idempotent and never deletes rows: diversified
aggregate rows written by the re-run are left in place.
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_synthetic_data import resolve_stack_resources  # noqa: E402

SNAPSHOT_FILE = (
    REPO_ROOT / "experiments" / "results"
    / "aggregates_snapshot_pre_diversified.json"
)


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


def to_jsonable(item):
    """Decimals/sets -> JSON-safe values."""

    from decimal import Decimal

    safe = {}

    for key, value in item.items():
        if isinstance(value, Decimal):
            safe[key] = float(value)
        elif value is None:
            continue
        else:
            safe[key] = value

    return safe


def from_jsonable(item):
    """JSON numbers -> Decimal so boto3 accepts them again."""

    from decimal import Decimal

    safe = {}

    for key, value in item.items():
        if isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, (int, float)):
            safe[key] = Decimal(str(value))
        else:
            safe[key] = value

    return safe


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Snapshot/restore DocumentAggregatesTable around "
            "a full-table aggregation run."
        )
    )
    parser.add_argument(
        "mode",
        choices=["snapshot", "restore"],
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
        "--exclude-mapping",
        default="experiments/results/"
        "diversified_500_mapping.json",
        help=(
            "Mapping whose api_document_ids keep their "
            "re-run aggregates during restore."
        ),
    )
    args = parser.parse_args()

    import boto3

    resources = resolve_stack_resources(
        args.stack_name, args.region
    )

    dynamodb = boto3.resource("dynamodb", region_name=args.region)

    aggregates_table = dynamodb.Table(
        resources["aggregates_table"]
    )

    if args.mode == "snapshot":
        items = scan_all_items(aggregates_table)

        SNAPSHOT_FILE.parent.mkdir(
            parents=True, exist_ok=True
        )

        SNAPSHOT_FILE.write_text(
            json.dumps(
                {
                    "snapshot_source_table": (
                        resources["aggregates_table"]
                    ),
                    "item_count": len(items),
                    "items": [
                        to_jsonable(item) for item in items
                    ],
                },
                indent=1,
            ),
            encoding="utf-8",
        )

        print(
            f"Snapshotted {len(items)} aggregate items to "
            f"{SNAPSHOT_FILE}"
        )
        return 0

    mapping_exclude = set()

    exclude_path = REPO_ROOT / args.exclude_mapping

    if exclude_path.exists():
        mapping_exclude = {
            entry["api_document_id"]
            for entry in json.loads(
                exclude_path.read_text(encoding="utf-8")
            )["documents"]
        }

    snapshot = json.loads(
        SNAPSHOT_FILE.read_text(encoding="utf-8")
    )

    restored = 0
    skipped = 0

    with aggregates_table.batch_writer() as batch:
        for item in snapshot["items"]:
            if item["document_id"] in mapping_exclude:
                skipped += 1
                continue

            batch.put_item(Item=from_jsonable(item))
            restored += 1

    print(
        f"Restored {restored} snapshot aggregate items "
        f"(skipped {skipped} diversified rows)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())