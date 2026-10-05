"""
Manual validation: run the repository's EXISTING optimizer against
the live DynamoDB aggregate record for a single document.

Usage (from the repository root):
    python scripts/validate_synthetic_0096.py

No optimizer logic is recreated here: the record goes through
optimization.adapters.from_aggregate_item
  -> optimization.optimizer.optimize_document
exactly as a future optimizer Lambda would.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.adapters import from_aggregate_item
from optimization.optimizer import optimize_document
from optimization.outputs import optimization_result_to_dict


# Live DocumentAggregatesTable item for synthetic-0096
# (verbatim values from the deployed table).
AGGREGATE_ITEM = {
    "document_id": "synthetic-0096",
    "file_size_bytes": 389000000,
    "current_storage_class": "STANDARD",
    "upload_timestamp": "2022-08-11T00:00:00Z",
    "last_accessed_timestamp": "2026-10-05T00:17:41+00:00",
    "access_count": 3,
    "days_since_last_access": 0.257690461087963,
    "access_frequency": 0.1,
    "aggregation_timestamp": "2026-10-05T00:17:41Z",
    "document_state": "ARCHIVED",
}


def main():
    # Step 2: bridge the raw aggregate item into OptimizerInput.
    document = from_aggregate_item(AGGREGATE_ITEM)

    print("Parsed OptimizerInput:")
    print(document)

    # Step 3: the actual repository optimizer.
    result = optimize_document(document)

    # Step 4: complete recommendation as dashboard-friendly JSON.
    print("\nFull optimizer result:")
    print(json.dumps(optimization_result_to_dict(result), indent=2))

    # Step 5: the recommended storage class.
    print("\n" + "=" * 60)
    print(f"RECOMMENDED STORAGE CLASS: {result.recommended_storage_class}")
    print(f"Eligible classes (ARCHIVED): {', '.join(result.eligible_storage_classes)}")

    # Step 6: costs and savings.
    print("=" * 60)
    print(f"Estimated current cost:     ${result.current_cost:.6f}")
    print(f"Estimated recommended cost: ${result.recommended_cost:.6f}")
    print(f"Estimated savings:          ${result.savings:.6f} "
          f"({result.savings_percentage:.2f}%)")
    print("=" * 60)

    print("\nCost breakdown per eligible tier (12-month projection):")

    for tier in result.tier_costs:
        cost = tier.cost
        print(
            f"  {tier.storage_class:<28}"
            f" total ${cost.total_cost:.6f}"
            f" (storage ${cost.storage_cost:.6f},"
            f" retrieval ${cost.retrieval_cost:.6f},"
            f" requests ${cost.request_cost:.6f},"
            f" transition ${cost.transition_cost:.6f})"
        )


if __name__ == "__main__":
    main()