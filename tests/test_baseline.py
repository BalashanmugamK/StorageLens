import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.baseline import calculate_baseline_cost
from optimization.models import OptimizerInput
from optimization.optimizer import optimize_document


REFERENCE_TIMESTAMP = "2026-09-11T00:00:00Z"


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


def test_baseline_comparison():
    total_current_cost = 0.0
    total_optimizer_cost = 0.0
    total_baseline_cost = 0.0

    print("\n" + "=" * 95)
    print("OPTIMIZER VS RIGOROUS AGE-BASED LIFECYCLE BASELINE")
    print("=" * 95)

    for number, scenario in enumerate(SCENARIOS, start=1):
        document = create_document(scenario, number)

        optimizer_result = optimize_document(document)

        baseline_result = calculate_baseline_cost(
            document,
            REFERENCE_TIMESTAMP,
        )

        optimizer_cost = optimizer_result.recommended_cost
        baseline_cost = baseline_result["total_cost"]

        total_current_cost += optimizer_result.current_cost
        total_optimizer_cost += optimizer_cost
        total_baseline_cost += baseline_cost

        print("\n" + "-" * 95)
        print(scenario["name"])
        print("-" * 95)

        print(
            f"Starting age:       "
            f"{baseline_result['age_days']:.0f} days"
        )

        print(
            f"30-day accesses:    "
            f"{document.access_count}"
        )

        print(
            f"Current tier:       "
            f"{document.current_storage_class}"
        )

        print(
            f"Optimizer tier:     "
            f"{optimizer_result.recommended_storage_class}"
        )

        print(
            f"Optimizer cost:     "
            f"${optimizer_cost:.6f}"
        )

        print(
            f"Baseline cost:      "
            f"${baseline_cost:.6f}"
        )

        print(
            f"Baseline transitions:"
            f" {baseline_result['transition_count']}"
        )

        difference = baseline_cost - optimizer_cost

        print(
            f"Baseline - optimizer:"
            f" ${difference:.6f}"
        )

        print("\nBaseline lifecycle periods:")

        for period in baseline_result["periods"]:
            print(
                f"  {period['storage_class']}: "
                f"{period['duration_days']:.2f} days "
                f""
                f"(age {period['start_age_days']:.2f}"
                f"-"
                f"{period['end_age_days']:.2f})"
            )

            print(
                f"    Storage:   "
                f"${period['storage_cost']:.6f}"
            )

            print(
                f"    Retrieval: "
                f"${period['retrieval_cost']:.6f}"
            )

            print(
                f"    Requests:  "
                f"${period['request_cost']:.6f}"
            )

        print("\nBaseline total breakdown:")

        print(
            f"  Storage:      "
            f"${baseline_result['storage_cost']:.6f}"
        )

        print(
            f"  Retrieval:    "
            f"${baseline_result['retrieval_cost']:.6f}"
        )

        print(
            f"  Requests:     "
            f"${baseline_result['request_cost']:.6f}"
        )

        print(
            f"  Transitions:  "
            f"${baseline_result['transition_cost']:.6f}"
        )

        print(
            f"  TOTAL:        "
            f"${baseline_result['total_cost']:.6f}"
        )

    print("\n" + "=" * 95)
    print("WORKLOAD COMPARISON")
    print("=" * 95)

    optimizer_savings = (
        total_current_cost
        - total_optimizer_cost
    )

    baseline_savings = (
        total_current_cost
        - total_baseline_cost
    )

    optimizer_savings_percentage = (
        optimizer_savings
        / total_current_cost
    ) * 100

    baseline_savings_percentage = (
        baseline_savings
        / total_current_cost
    ) * 100

    optimizer_vs_baseline = (
        total_baseline_cost
        - total_optimizer_cost
    )

    print(
        f"Current workload cost:   "
        f"${total_current_cost:.6f}"
    )

    print(
        f"Optimizer workload cost: "
        f"${total_optimizer_cost:.6f}"
    )

    print(
        f"Baseline workload cost:  "
        f"${total_baseline_cost:.6f}"
    )

    print(
        f"\nOptimizer savings vs current:"
        f" ${optimizer_savings:.6f}"
    )

    print(
        f"Optimizer savings %:           "
        f"{optimizer_savings_percentage:.2f}%"
    )

    print(
        f"\nBaseline savings vs current:   "
        f"${baseline_savings:.6f}"
    )

    print(
        f"Baseline savings %:            "
        f"{baseline_savings_percentage:.2f}%"
    )

    print(
        f"\nOptimizer advantage vs baseline:"
        f" ${optimizer_vs_baseline:.6f}"
    )


if __name__ == "__main__":
    test_baseline_comparison()
