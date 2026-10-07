import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.adapters import from_aggregate_item
from optimization.constraints import get_eligible_storage_classes
from optimization.metrics import calculate_workload_summary
from optimization.models import OptimizerInput
from optimization.optimizer import POLICY_A, POLICY_B, optimize_document
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
        "expected_recommendation": "STANDARD",
        "expected_verdict": "NO CHANGE",
    },
    {
        "name": "2. Occasionally accessed ACTIVE document",
        "state": "ACTIVE",
        "access_count": 5,
        "current": "STANDARD",
        "upload_timestamp": "2026-08-01T00:00:00Z",
        "expected_recommendation": "INTELLIGENT_TIERING",
        "expected_verdict": "POSITIVE SAVINGS",
    },
    {
        "name": "3. Rarely accessed CLOSED document",
        "state": "CLOSED",
        "access_count": 1,
        "current": "STANDARD_IA",
        "upload_timestamp": "2026-05-01T00:00:00Z",
        "expected_recommendation": "GLACIER_FLEXIBLE_RETRIEVAL",
        "expected_verdict": "POSITIVE SAVINGS",
    },
    {
        "name": "4. Very rarely accessed ARCHIVED document",
        "state": "ARCHIVED",
        "access_count": 0,
        "current": "GLACIER_FLEXIBLE_RETRIEVAL",
        "upload_timestamp": "2026-01-01T00:00:00Z",
        "expected_recommendation": "GLACIER_DEEP_ARCHIVE",
        "expected_verdict": "POSITIVE SAVINGS",
    },
    {
        "name": "5. Old but frequently accessed ARCHIVED document",
        "state": "ARCHIVED",
        "access_count": 30,
        "current": "GLACIER_DEEP_ARCHIVE",
        "upload_timestamp": "2026-01-01T00:00:00Z",
        "expected_recommendation": "INTELLIGENT_TIERING",
        "expected_verdict": "POSITIVE SAVINGS",
    },
]


def classify(savings):
    if abs(savings) <= 1e-6:
        return "NO CHANGE"
    if savings > 0:
        return "POSITIVE SAVINGS"
    return "NEGATIVE SAVINGS"


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


def test_optimizer_scenarios():
    """
    Regression-pinning scenario test: every scenario must keep
    its expected recommendation, verdict, policy fields, and
    savings behaviour under the default cost-safe policy B.
    """

    documents = []
    results = []

    for number, scenario in enumerate(SCENARIOS, start=1):
        document = create_document(scenario, number)
        result = optimize_document(
            document,
            policy=POLICY_B,
        )

        assert (
            result.recommended_storage_class
            == scenario["expected_recommendation"]
        ), scenario["name"]

        assert result.policy == POLICY_B, scenario["name"]
        assert result.policy_conflict is False, scenario["name"]

        verdict = classify(result.savings)
        assert (
            verdict == scenario["expected_verdict"]
        ), scenario["name"]

        assert result.savings >= 0.0, scenario["name"]
        assert math.isclose(
            result.savings,
            result.current_cost - result.recommended_cost,
            rel_tol=0.0,
            abs_tol=1e-12,
        ), scenario["name"]

        assert (
            result.candidate_storage_classes
            == get_eligible_storage_classes(
                scenario["state"]
            )
        ), scenario["name"]

        assert (
            result.recommended_retrieval_time_hours
            >= 0.0
        ), scenario["name"]

        documents.append(document)
        results.append(result)

    # Policy A reaches the same decisions for these scenarios:
    # every scenario's current class is inside its state's
    # eligibility set, so no policy conflict exists.
    for number, scenario in enumerate(SCENARIOS, start=1):
        document = create_document(scenario, number)
        result_a = optimize_document(
            document,
            policy=POLICY_A,
        )

        assert (
            result_a.recommended_storage_class
            == scenario["expected_recommendation"]
        ), scenario["name"]
        assert result_a.policy == POLICY_A
        assert result_a.policy_conflict is False

    summary = calculate_workload_summary(
        documents,
        results,
    )

    assert summary.policy_conflict_count == 0
    assert math.isclose(
        summary.total_savings,
        sum(result.savings for result in results),
        rel_tol=1e-9,
    )


def test_optimizer_output_serialization():
    """The result/summary dicts expose the new policy fields."""

    result = optimize_document(
        create_document(SCENARIOS[0], 0)
    )

    result_json = optimization_result_to_dict(result)

    assert result_json["policy"] == POLICY_B
    assert result_json["policy_conflict"] is False
    assert (
        result_json["candidate_storage_classes"]
        is not None
    )
    assert any(
        "retrieval_time_hours" in tier
        for tier in result_json["tier_costs"]
    )

    summary = calculate_workload_summary(
        [create_document(SCENARIOS[0], 0)],
        [result],
    )

    summary_json = workload_summary_to_dict(summary)

    assert "policy_conflict_count" in summary_json


def print_cost_breakdown(result):
    print("\nCost breakdown:")

    for tier in result.tier_costs:
        print(f"\n  {tier.storage_class}")
        print(f"    Storage:     ${tier.cost.storage_cost:.6f}")
        print(f"    Retrieval:   ${tier.cost.retrieval_cost:.6f}")
        print(f"    Requests:    ${tier.cost.request_cost:.6f}")
        print(f"    Transition:  ${tier.cost.transition_cost:.6f}")
        print(f"    TOTAL:       ${tier.cost.total_cost:.6f}")


def main():
    pytest.main([__file__, "-v", "-s"])


if __name__ == "__main__":
    main()