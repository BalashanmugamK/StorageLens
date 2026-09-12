@'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.adapters import from_aggregate_item
from optimization.metrics import calculate_workload_summary
from optimization.models import OptimizerInput
from optimization.optimizer import optimize_document
from optimization.outputs import (
    optimization_result_to_dict,
    workload_summary_to_dict,
)


SCENARIOS = [
    {
        "name": "1. Frequently accessed ACTIVE document",
        "state": "ACTIVE",
        "access_count": 60,
        "current": "STANDARD",
        "upload_timestamp": "2026-08-20T00:00:00Z",
    },
    {
        "name": "2. Occasionally accessed ACTIVE document",
        "state": "ACTIVE",
        "access_count": 5,
        "current": "STANDARD",
        "upload_timestamp": "2026-08-01T00:00:00Z",
    },
    {
        "name": "3. Rarely accessed CLOSED document",
        "state": "CLOSED",
        "access_count": 1,
        "current": "STANDARD_IA",
        "upload_timestamp": "2026-05-01T00:00:00Z",
    },
    {
        "name": "4. Very rarely accessed ARCHIVED document",
        "state": "ARCHIVED",
        "access_count": 0,
        "current": "GLACIER_FLEXIBLE_RETRIEVAL",
        "upload_timestamp": "2026-01-01T00:00:00Z",
    },
    {
        "name": "5. Old but frequently accessed ARCHIVED document",
        "state": "ARCHIVED",
        "access_count": 30,
        "current": "GLACIER_DEEP_ARCHIVE",
        "upload_timestamp": "2026-01-01T00:00:00Z",
    },
]


def create_document(scenario, number):
    access_count = scenario["access_count"]

    if access_count > 0:
        last_accessed = "2026-09-09T00:00:00Z"
        days_since_last_access = 1
    else:
        last_accessed = None
        days_since_last_access = None

    return OptimizerInput(
        document_id=f"scenario-{number}",
        file_size_bytes=1_000_000_000,
        current_storage_class=scenario["current"],
        upload_timestamp=scenario["upload_timestamp"],
        last_accessed_timestamp=last_accessed,
        access_count=access_count,
        days_since_last_access=days_since_last_access,
        access_frequency=access_count / 30,
        aggregation_timestamp="2026-09-10T00:00:00Z",
        document_state=scenario["state"],
    )


def print_cost_breakdown(result):
    print("\nCost breakdown:")

    for tier in result.tier_costs:
        print(f"\n  {tier.storage_class}")
        print(f"    Storage:     ${tier.cost.storage_cost:.6f}")
        print(f"    Retrieval:   ${tier.cost.retrieval_cost:.6f}")
        print(f"    Requests:    ${tier.cost.request_cost:.6f}")
        print(f"    Transition:  ${tier.cost.transition_cost:.6f}")
        print(f"    TOTAL:       ${tier.cost.total_cost:.6f}")


def test_optimizer_scenarios():
    documents = []
    results = []

    for number, scenario in enumerate(SCENARIOS, start=1):
        document = create_document(scenario, number)
        result = optimize_document(document)

        documents.append(document)
        results.append(result)

        print("\n" + "=" * 75)
        print(scenario["name"])
        print("=" * 75)

        print(f"State:              {scenario['state']}")
        print(f"Upload timestamp:   {document.upload_timestamp}")
        print(f"30-day accesses:    {scenario['access_count']}")
        print(f"Access frequency:   {document.access_frequency:.4f}/day")
        print(f"Current tier:       {result.current_storage_class}")

        print(
            f"Eligible tiers:     "
            f"{', '.join(result.eligible_storage_classes)}"
        )

        print(
            f"Recommended tier:   "
            f"{result.recommended_storage_class}"
        )

        print(f"Current cost:       ${result.current_cost:.6f}")
        print(f"Recommended cost:   ${result.recommended_cost:.6f}")
        print(f"Savings:            ${result.savings:.6f}")

        print(
            f"Savings percentage: "
            f"{result.savings_percentage:.2f}%"
        )

        print_cost_breakdown(result)

    print("\n" + "=" * 75)
    print("CLEAN JSON OUTPUT")
    print("=" * 75)

    first_result_json = optimization_result_to_dict(results[0])

    print(
        json.dumps(
            first_result_json,
            indent=2,
        )
    )

    print("\n" + "=" * 75)
    print("WORKLOAD SUMMARY")
    print("=" * 75)

    summary = calculate_workload_summary(
        documents,
        results,
    )

    summary_json = workload_summary_to_dict(summary)

    print(
        json.dumps(
            summary_json,
            indent=2,
        )
    )

    print("\n" + "=" * 75)
    print("ADAPTER TEST")
    print("=" * 75)

    aggregate_item = {
        "document_id": "adapter-test-001",
        "file_size_bytes": 1_000_000_000,
        "current_storage_class": "STANDARD_IA",
        "upload_timestamp": "2026-08-01T10:00:00Z",
        "last_accessed_timestamp": "2026-09-01T10:00:00Z",
        "access_count": 1,
        "days_since_last_access": 10.0,
        "access_frequency": 1 / 30,
        "aggregation_timestamp": "2026-09-11T10:00:00Z",
        "document_state": "CLOSED",
    }

    adapter_document = from_aggregate_item(aggregate_item)

    print("Aggregate item converted successfully:")
    print(adapter_document)


if __name__ == "__main__":
    test_optimizer_scenarios()
'@ | Set-Content tests\test_optimizer_scenarios.py