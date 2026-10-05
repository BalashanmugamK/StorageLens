"""
Fidelity tests for the cost-model fixes:

  - minimum billable object size (128 KB on SIA/GIR, 32 KB floor
    plus 40 KB overhead on Flexible/Deep Archive)
  - prorated early-deletion fee when leaving the current class
    inside its minimum storage duration
  - recency-attenuated annual download forecast
  - Policy A (state-constrained) vs Policy B (cost-safe, with
    the POLICY_CONFLICT flag and non-negative savings)

Every expected number is derived independently from the AWS
billing rules in test comments, not by re-calling the engine.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.access import (
    calculate_expected_annual_downloads,
)
from optimization.adapters import from_aggregate_item
from optimization.costs import (
    calculate_tier_cost,
    get_retrieval_time_hours,
    warn_if_pricing_stale,
)
from optimization.models import OptimizerInput
from optimization.optimizer import (
    POLICY_A,
    POLICY_B,
    optimize_document,
)


GB_BYTES = 1_000_000_000

# All docs share these reference timestamps so expectations are
# exact; the upload timestamp varies to control object age.
AGGREGATION_TS = "2026-09-10T00:00:00Z"


def make_document(
    file_size_bytes=GB_BYTES,
    current="STANDARD",
    state=None,
    access_count=0,
    days_since_last_access=None,
    upload="2026-01-01T00:00:00Z",
):
    return OptimizerInput(
        document_id="fidelity-test",
        file_size_bytes=file_size_bytes,
        current_storage_class=current,
        upload_timestamp=upload,
        last_accessed_timestamp=None,
        access_count=access_count,
        days_since_last_access=days_since_last_access,
        access_frequency=access_count / 30,
        aggregation_timestamp=AGGREGATION_TS,
        document_state=state,
    )


def tier_cost(result, storage_class):
    return next(
        tier.cost
        for tier in result.tier_costs
        if tier.storage_class == storage_class
    )


# AWS bills SIA objects at a 128 KB minimum: a 50 KB object is
# billed on 128 KB. Storage = (128000/1e9) * 0.0138 * 12
# = 2.11968e-05; moving from Standard adds 0.01/1000 = 1e-05.
def test_sia_minimum_billable_object_size():
    document = make_document(file_size_bytes=50_000)

    cost = calculate_tier_cost(
        document,
        "STANDARD_IA",
        include_transition=True,
    )

    expected_storage = (128_000 / GB_BYTES) * 0.0138 * 12

    assert cost.storage_cost == pytest.approx(
        expected_storage, rel=1e-9
    )

    smaller_billing = (50_000 / GB_BYTES) * 0.0138 * 12

    assert cost.storage_cost > smaller_billing * 2

    assert cost.total_cost == pytest.approx(
        expected_storage + 0.01 / 1000, rel=1e-9
    )


# Flexible Retrieval adds 40 KB archived-object overhead on top
# of the object: 32 KB at the archive rate, 8 KB at Standard
# rates. Storage = ((1e6+32k)/1e9)*0.0045*12
#                + (8k/1e9) * 0.025 * 12 = 5.8128e-05.
def test_archive_40kb_overhead_flexible():
    document = make_document(file_size_bytes=1_000_000)

    cost = calculate_tier_cost(
        document,
        "GLACIER_FLEXIBLE_RETRIEVAL",
        include_transition=True,
    )

    expected_storage = (
        (1_032_000 / GB_BYTES) * 0.0045 * 12
        + (8_000 / GB_BYTES) * 0.025 * 12
    )

    assert cost.storage_cost == pytest.approx(
        expected_storage, rel=1e-9
    )

    assert cost.total_cost == pytest.approx(
        expected_storage + 0.036 / 1000, rel=1e-9
    )


# Leaving SIA after only 10 days of a 30-day minimum bills the
# 20 remaining days prorated at the SIA storage rate on the
# billable size: 20 * (0.0138/30) * 1.0 = 0.0092. The Deep
# Archive transition request adds 0.07/1000 = 0.00007.
def test_early_deletion_fee_current_tier():
    document = make_document(
        current="STANDARD_IA",
        upload="2026-08-31T00:00:00Z",
    )

    cost = calculate_tier_cost(
        document,
        "GLACIER_DEEP_ARCHIVE",
        include_transition=True,
    )

    expected_fee = (
        20 * (0.0138 / 30) * 1.0
    )

    assert cost.transition_cost == pytest.approx(
        expected_fee + 0.07 / 1000, rel=1e-9
    )

    # Past the minimum duration, only the transition request
    # remains.
    old_document = make_document(
        current="STANDARD_IA",
        upload="2026-06-01T00:00:00Z",
    )

    old_cost = calculate_tier_cost(
        old_document,
        "GLACIER_DEEP_ARCHIVE",
        include_transition=True,
    )

    assert old_cost.transition_cost == pytest.approx(
        0.07 / 1000, rel=1e-9
    )

    # Staying put never bills the early-deletion fee.
    staying = calculate_tier_cost(
        document,
        "STANDARD_IA",
        include_transition=False,
    )

    assert staying.transition_cost == 0.0


# The annual forecast is the 30-day frequency x 365, attenuated
# by an exponential recency weight with a 30-day half life:
# weight(1 day) ~= 0.9772, weight(28 days) ~= 0.5235.
def test_recency_attenuated_forecast():
    assert calculate_expected_annual_downloads(
        1.0
    ) == pytest.approx(365.0)

    assert calculate_expected_annual_downloads(
        1.0, None, 30
    ) == pytest.approx(365.0)

    stale = calculate_expected_annual_downloads(
        1.0, 28, 30
    )

    fresh = calculate_expected_annual_downloads(
        1.0, 1, 30
    )

    assert fresh == pytest.approx(
        365 * 0.5 ** (1 / 30), rel=1e-9
    )

    assert stale == pytest.approx(
        365 * 0.5 ** (28 / 30), rel=1e-9
    )

    assert stale / fresh == pytest.approx(
        0.5 ** (27 / 30), rel=1e-9
    )


# The pasted example: current Standard, state ARCHIVED. The
# cheapest state-eligible tier is far cheaper than Standard, so
# BOTH policies move the object to Deep Archive and report the
# state-policy conflict via POLICY_CONFLICT.
def test_policy_conflict_when_current_ineligible():
    document = make_document(state="ARCHIVED")

    for policy in (POLICY_A, POLICY_B):
        result = optimize_document(
            document, policy=policy
        )

        assert (
            result.recommended_storage_class
            == "GLACIER_DEEP_ARCHIVE"
        )
        assert result.savings > 0
        assert result.policy == policy
        assert result.policy_conflict is True


# Negative-savings case: current SIA, state ARCHIVED, high
# retrieval load. Every state-eligible class costs MORE than
# keeping SIA, so Policy A still forces the move (negative
# savings), while Policy B keeps the object and flags the
# conflict - the exact gap the Policy B definition closes.
def test_policy_b_never_recommends_cost_increase():
    document = make_document(
        current="STANDARD_IA",
        state="ARCHIVED",
        access_count=30,
        days_since_last_access=1,
    )

    result_a = optimize_document(
        document, policy=POLICY_A
    )

    assert (
        result_a.recommended_storage_class
        == "GLACIER_FLEXIBLE_RETRIEVAL"
    )
    assert result_a.savings < -0.5
    assert result_a.policy_conflict is True

    result_b = optimize_document(
        document, policy=POLICY_B
    )

    assert (
        result_b.recommended_storage_class
        == "STANDARD_IA"
    )
    assert result_b.savings == 0.0
    assert result_b.current_cost == pytest.approx(
        result_b.recommended_cost, rel=1e-9
    )
    assert result_b.policy_conflict is True
    assert (
        "STANDARD_IA"
        in result_b.candidate_storage_classes
    )
    assert (
        result_b.candidate_storage_classes
        == result_b.eligible_storage_classes
        + ["STANDARD_IA"]
    )


# When the current class is eligible and already cheapest, the
# engine keeps it and reports NO CHANGE (tie handling).
def test_policy_b_keeps_cheapest_current_class():
    document = make_document(
        current="GLACIER_FLEXIBLE_RETRIEVAL",
        state="CLOSED",
    )

    result = optimize_document(
        document, policy=POLICY_B
    )

    assert (
        result.recommended_storage_class
        == "GLACIER_FLEXIBLE_RETRIEVAL"
    )
    assert result.savings == 0.0
    assert result.policy_conflict is False


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        optimize_document(make_document(), policy="C")


# Restore waits are surfaced informationally: Deep Archive
# standard retrieval ~12 h, Flexible ~5 h, online classes 0.
def test_retrieval_time_surfaced():
    document = make_document(state="ARCHIVED")

    result = optimize_document(
        document, policy=POLICY_B
    )

    assert (
        result.recommended_retrieval_time_hours >= 12.0
    )  # Deep Archive recommendation

    hours = {
        tier.storage_class: tier.retrieval_time_hours
        for tier in result.tier_costs
    }

    # tier_costs covers the Policy B candidate set: the
    # ARCHIVED-eligible classes plus the current class.
    assert set(hours) == {
        "GLACIER_INSTANT_RETRIEVAL",
        "GLACIER_FLEXIBLE_RETRIEVAL",
        "GLACIER_DEEP_ARCHIVE",
        "STANDARD",
    }

    assert hours["GLACIER_INSTANT_RETRIEVAL"] == 0.0
    assert hours["GLACIER_FLEXIBLE_RETRIEVAL"] >= 5.0
    assert (
        get_retrieval_time_hours("STANDARD_IA") == 0.0
    )
    assert (
        get_retrieval_time_hours("GLACIER_DEEP_ARCHIVE")
        >= 12.0
    )


def test_adapter_keeps_state_and_fields():
    item = {
        "document_id": "adapter-001",
        "file_size_bytes": 1_000_000,
        "current_storage_class": "STANDARD_IA",
        "upload_timestamp": "2026-08-01T10:00:00Z",
        "last_accessed_timestamp": "2026-09-01T10:00:00Z",
        "access_count": 1,
        "days_since_last_access": "10.0",
        "access_frequency": "0.0333333333",
        "aggregation_timestamp": "2026-09-11T10:00:00Z",
        "document_state": "closed",
    }

    document = from_aggregate_item(item)

    assert document.document_state == "CLOSED"
    assert document.days_since_last_access == 10.0


# The snapshot carries a freshness budget; a stale date raises
# a UserWarning once, then stays quiet (module-global flag).
def test_pricing_staleness_warning():
    import warnings

    import optimization.costs as costs

    stale = {
        "pricing_checked": "2026-01-01",
        "model_assumptions": {
            "pricing_max_age_days": 90
        },
        "storage_classes": {},
    }

    costs._STALENESS_WARNED = False

    try:
        with pytest.warns(UserWarning):
            costs.warn_if_pricing_stale(stale)

        # Warn-once: the second call emits nothing.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            costs.warn_if_pricing_stale(stale)
    finally:
        # Restore pristine state for later tests, but mark it
        # clean so a fresh warning can fire again if needed.
        costs._STALENESS_WARNED = False