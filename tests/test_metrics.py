"""Workload-summary metrics tests.

Hand-derived expected values from optimization/pricing.json
(ap-south-1). The Policy A case is the regression test for the
defect where current_retrieval_cost was silently 0: under Policy
A the current class is not state-eligible, never appears in
tier_costs, and must be re-derived from the cost model instead.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.metrics import calculate_workload_summary
from optimization.models import OptimizerInput


GB_BYTES = 1_000_000_000

AGGREGATION_TS = "2026-09-10T00:00:00Z"


def make_document(
    *,
    document_id: str,
    current: str,
    state: str | None,
    access_count: int,
    file_size_bytes: int = GB_BYTES,
    days_since_last_access: float | None = None,
) -> OptimizerInput:
    return OptimizerInput(
        document_id=document_id,
        file_size_bytes=file_size_bytes,
        current_storage_class=current,
        upload_timestamp="2026-01-01T00:00:00Z",
        last_accessed_timestamp=None,
        access_count=access_count,
        days_since_last_access=days_since_last_access,
        access_frequency=access_count / 30,
        aggregation_timestamp=AGGREGATION_TS,
        document_state=state,
    )


def test_current_retrieval_cost_under_policy_a():
    """ARCHIVED state excludes Standard and Standard-IA from the
    candidate set. A Standard-IA document optimized under Policy A
    has no current-class row in tier_costs, so the summary must
    re-derive the current class's economics. With no recency
    input the forecast is 1/day x 365 = 365 downloads/yr; the
    current class (Standard-IA) retrieval fee is
    365 GB x 0.01/GB = 3.65 — not zero."""

    document = make_document(
        document_id="metrics-a",
        current="STANDARD_IA",
        state="ARCHIVED",
        access_count=30,
    )

    from optimization.optimizer import (
        POLICY_A,
        optimize_document,
    )

    result = optimize_document(document, policy=POLICY_A)

    # The current class must genuinely be absent from the
    # candidates for this test to exercise the defect.
    assert "STANDARD_IA" not in result.candidate_storage_classes
    assert all(
        tier.storage_class != "STANDARD_IA"
        for tier in result.tier_costs
    )

    summary = calculate_workload_summary([document], [result])

    assert summary.current_retrieval_cost == pytest.approx(
        365.0 * 0.01,  # 365 downloads/yr x SIA retrieval per GB
        rel=1e-9,
    )

    # The recommended class's retrieval cost is still read from
    # tier_costs (it is always a candidate).
    recommended_retrieval = next(
        tier.cost.retrieval_cost
        for tier in result.tier_costs
        if tier.storage_class
        == result.recommended_storage_class
    )

    assert summary.optimized_retrieval_cost == pytest.approx(
        recommended_retrieval, rel=1e-6
    )


def test_current_retrieval_cost_under_policy_b_unchanged():
    """Under Policy B the current class is a candidate, so the
    summary reads its tier_costs row (no re-derivation) and the
    numbers must agree with the direct read."""

    document = make_document(
        document_id="metrics-b",
        current="STANDARD_IA",
        state="ARCHIVED",
        access_count=30,
    )

    from optimization.optimizer import optimize_document

    result = optimize_document(document)  # default Policy B

    direct = next(
        tier.cost.retrieval_cost
        for tier in result.tier_costs
        if tier.storage_class == "STANDARD_IA"
    )

    summary = calculate_workload_summary([document], [result])

    assert math.isclose(
        summary.current_retrieval_cost, direct, rel_tol=1e-9
    )


def test_summary_totals_arithmetic_identity():
    document = make_document(
        document_id="totals",
        current="GLACIER_FLEXIBLE_RETRIEVAL",
        state="ARCHIVED",
        access_count=0,
        file_size_bytes=50_000_000,
    )

    from optimization.optimizer import optimize_document

    result = optimize_document(document)

    summary = calculate_workload_summary([document], [result])

    assert math.isclose(
        summary.total_savings,
        summary.current_cost - summary.optimized_cost,
        rel_tol=1e-9,
    )

    assert summary.total_documents == 1