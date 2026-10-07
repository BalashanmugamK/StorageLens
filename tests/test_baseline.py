"""Baseline cost-model tests with hand-derived expected values.

The billing arithmetic expected here is derived directly from AWS
billing rules and optimization/pricing.json (NOT recomputed via the
implementation under test), the same discipline as
tests/test_cost_model_fidelity.py:

  - billable size floors: 128 KB on Standard-IA / Glacier Instant
    Retrieval; 32 KB on Glacier Flexible / Deep Archive plus the
    40 KB archived-object overhead (32 KB at the archive rate,
    8 KB at Standard rates).
  - early-deletion fee: remaining minimum-duration days at the
    current class's storage rate on its billable size
    (SIA 30 days → daily rate 0.0138/30).
  - forecast: 30-day frequency x 365, attenuated by
    0.5 ** (days_since_last_access / 30).

Pricing constants below are quoted from optimization/pricing.json
for ap-south-1. If the snapshot is legitimately refreshed, update
these literals in the same commit as pricing.json.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optimization.baseline import (
    calculate_baseline_cost,
    calculate_lifecycle_periods,
)
from optimization.optimizer import optimize_document
from optimization.models import OptimizerInput


REFERENCE_TIMESTAMP = "2026-09-11T00:00:00Z"

KB = 1_000

# pricing.json (ap-south-1) storage rates, per GB-month.
STANDARD_RATE = 0.025
SIA_RATE = 0.0138
FLEXIBLE_RATE = 0.0045
DEEP_RATE = 0.002

SIA_MIN_BYTES = 128 * KB
ARCHIVE_MIN_BYTES = 32 * KB
ARCHIVE_GLACIER_OVERHEAD_BYTES = 32 * KB
ARCHIVE_STANDARD_OVERHEAD_BYTES = 8 * KB

SIA_TRANSITION_PER_1000 = 0.01
FLEXIBLE_TRANSITION_PER_1000 = 0.036
DEEP_TRANSITION_PER_1000 = 0.07

SIA_MIN_DAYS = 30


def create_document(
    *,
    file_size_bytes: int,
    current: str,
    upload_timestamp: str,
    access_count: int = 0,
    days_since_last_access: float | None = None,
    state: str | None = None,
) -> OptimizerInput:
    if access_count > 0 and days_since_last_access is None:
        days_since_last_access = 1.0

    return OptimizerInput(
        document_id="test-document",
        file_size_bytes=file_size_bytes,
        current_storage_class=current,
        upload_timestamp=upload_timestamp,
        last_accessed_timestamp=(
            "2026-08-22T00:00:00Z"
            if access_count > 0
            else None
        ),
        access_count=access_count,
        days_since_last_access=days_since_last_access,
        access_frequency=access_count / 30,
        aggregation_timestamp="2026-09-10T00:00:00Z",
        document_state=state,
    )


def gb(bytes_: float) -> float:
    return bytes_ / 1_000_000_000


def months(duration_days: float) -> float:
    return duration_days * 12 / 365


def test_new_document_follows_the_age_ladder():
    """A document uploaded at the reference point spends 30 days
    in Standard, 60 in Standard-IA, 90 in Glacier Flexible and
    the rest of the 12-month horizon in Deep Archive."""

    periods = calculate_lifecycle_periods(
        0.0, 365
    )

    shapes = [
        (periods[0], "STANDARD", 30.0),
        (periods[1], "STANDARD_IA", 60.0),
        (periods[2], "GLACIER_FLEXIBLE_RETRIEVAL", 90.0),
        (periods[3], "GLACIER_DEEP_ARCHIVE", 185.0),
    ]

    assert len(periods) == 4

    for period, storage_class, duration in shapes:
        assert period["storage_class"] == storage_class
        assert math.isclose(
            period["duration_days"], duration, rel_tol=1e-9
        )

    assert (
        sum(
            period["duration_days"]
            for period in periods
        )
        == 365.0
    )


def test_baseline_bills_the_128kb_ia_floor():
    """A 10 KB object in the baseline's Standard-IA period must be
    billed on 128 KB, exactly as the optimizer's per-tier model
    bills it (and as AWS bills it). Hand-derived:

        storage
          = gb(10_000) x 0.025 x months(30)          (Standard)
          + gb(128_000) x 0.0138 x months(60)        (Standard-IA)
          + gb(72_000) x 0.0045 x months(90)         (Flex archive rate)
          + gb(8_000) x 0.025 x months(90)           (Flex standard part)
          + gb(72_000) x 0.002 x months(185)         (Deep archive rate)
          + gb(8_000) x 0.025 x months(185)          (Deep standard part)

    where Flex/Deep billable = max(10 KB, 32 KB) + 32 KB overhead
    = 72 KB, and there are no accesses so retrieval/request costs
    are all zero.
    """

    document = create_document(
        file_size_bytes=10 * KB,
        current="STANDARD",
        upload_timestamp=REFERENCE_TIMESTAMP,
    )

    result = calculate_baseline_cost(
        document,
        REFERENCE_TIMESTAMP,
    )

    expected_storage = (
        gb(10 * KB) * STANDARD_RATE * months(30)
        + gb(SIA_MIN_BYTES) * SIA_RATE * months(60)
        + gb(ARCHIVE_MIN_BYTES + ARCHIVE_GLACIER_OVERHEAD_BYTES)
        * FLEXIBLE_RATE
        * months(90)
        + gb(ARCHIVE_STANDARD_OVERHEAD_BYTES) * STANDARD_RATE * months(90)
        + gb(ARCHIVE_MIN_BYTES + ARCHIVE_GLACIER_OVERHEAD_BYTES)
        * DEEP_RATE
        * months(185)
        + gb(ARCHIVE_STANDARD_OVERHEAD_BYTES) * STANDARD_RATE * months(185)
    )

    assert math.isclose(
        result["storage_cost"],
        expected_storage,
        rel_tol=1e-9,
    )

    assert math.isclose(
        result["retrieval_cost"], 0.0, abs_tol=0.0
    )

    assert math.isclose(result["request_cost"], 0.0, abs_tol=0.0)


def test_baseline_charges_early_deletion_for_leaving_sia():
    """An object that spent 10 days in Standard-IA before the
    baseline moves it back to Standard pays the remaining 20 days
    of the 30-day minimum on the 128 KB billable size:

        fee = 20 x (0.0138 / 30) x gb(128_000)

    Subsequent ladder transitions (SIA, Flexible, Deep at ages
    30/90/180) add their transition PUT charges; the move into
    Standard itself is free (Standard has no transition fee).
    """

    document = create_document(
        file_size_bytes=10 * KB,
        current="STANDARD_IA",
        # Uploaded 2026-08-31, aggregated 2026-09-10 → 10 days held
        # in Standard-IA when the baseline moves it to Standard
        # (its age at the reference point is 11 days, so the ladder
        # starts with a Standard period).
        upload_timestamp="2026-08-31T00:00:00Z",
    )

    expected_fee = (
        (SIA_MIN_DAYS - 10)
        * (SIA_RATE / 30)
        * gb(SIA_MIN_BYTES)
    )

    result = calculate_baseline_cost(
        document,
        REFERENCE_TIMESTAMP,
    )

    expected_transition_cost = (
        expected_fee
        + SIA_TRANSITION_PER_1000 / 1000
        + FLEXIBLE_TRANSITION_PER_1000 / 1000
        + DEEP_TRANSITION_PER_1000 / 1000
    )

    assert math.isclose(
        result["transition_cost"],
        expected_transition_cost,
        rel_tol=1e-9,
    )

    assert result["transition_count"] == 4


def test_baseline_uses_the_recency_attenuated_forecast():
    """The baseline's forecast must match the optimizer's: the
    30-day frequency x 365, attenuated by 0.5 ** (days/30).

    For 30 accesses in the window with the last access 20 days
    before the aggregation point:

        weight        = 0.5 ** (20 / 30)
        annual        = (30 / 30) x 365 x weight
        downloads_30d = annual x 30 / 365   (first period)

    Requests in that period = downloads_30d / 1000 x 0.0004
    (Standard's GET rate).
    """

    last_access = "2026-08-21T00:00:00Z"  # 20 days before 2026-09-10

    last_access = "2026-08-21T00:00:00Z"  # 20 days before 2026-09-10

    document = create_document(
        file_size_bytes=1_000 * KB,
        current="STANDARD",
        upload_timestamp=REFERENCE_TIMESTAMP,  # age 0: a clean 30-day first Standard period
        access_count=30,
        days_since_last_access=20.0,
        state="ACTIVE",
    )

    result = calculate_baseline_cost(
        document,
        REFERENCE_TIMESTAMP,
    )

    first_period = result["periods"][0]

    weight = 0.5 ** (20 / 30)

    expected_downloads = (30 / 30) * 365 * weight * 30 / 365

    assert math.isclose(
        first_period["download_count"],
        expected_downloads,
        rel_tol=1e-9,
    )

    assert math.isclose(
        first_period["request_cost"],
        expected_downloads / 1000 * 0.0004,
        rel_tol=1e-9,
    )


def test_baseline_total_equals_component_sum():
    """Arithmetic-identity guard across all scenarios: the reported
    total must equal storage + retrieval + requests + transitions."""

    scenarios = [
        dict(
            file_size_bytes=1_000_000_000,
            current="STANDARD",
            upload_timestamp="2026-08-20T00:00:00Z",
            access_count=60,
            state="ACTIVE",
        ),
        dict(
            file_size_bytes=50 * KB,
            current="STANDARD_IA",
            upload_timestamp="2026-05-01T00:00:00Z",
            access_count=1,
            state="CLOSED",
        ),
        dict(
            file_size_bytes=300 * KB,
            current="GLACIER_DEEP_ARCHIVE",
            upload_timestamp="2026-01-01T00:00:00Z",
            access_count=30,
            state="ARCHIVED",
        ),
    ]

    for index, spec in enumerate(scenarios, start=1):
        document = create_document(**spec)

        result = calculate_baseline_cost(
            document,
            REFERENCE_TIMESTAMP,
        )

        assert result["total_cost"] == math.fsum(
            [
                result["storage_cost"],
                result["retrieval_cost"],
                result["request_cost"],
                result["transition_cost"],
            ]
        ), f"component mismatch in scenario {index}"

        assert result["total_cost"] > 0.0


def test_baseline_comparison_report():
    """End-to-end comparison against the optimizer, now with
    assertions: every scenario must produce finite, positive
    costs on both sides, and the optimizer's policy-B result
    must never exceed its own current-class cost."""

    SCENARIOS = [
        {
            "name": 1,
            "file_size_bytes": 1_000_000_000,
            "current": "STANDARD",
            "upload_timestamp": "2026-08-20T00:00:00Z",
            "access_count": 60,
            "state": "ACTIVE",
        },
        {
            "name": 2,
            "file_size_bytes": 500 * KB,
            "current": "STANDARD",
            "upload_timestamp": "2026-08-01T00:00:00Z",
            "access_count": 5,
            "state": "ACTIVE",
        },
        {
            "name": 3,
            "file_size_bytes": 50 * KB,
            "current": "STANDARD_IA",
            "upload_timestamp": "2026-05-01T00:00:00Z",
            "access_count": 1,
            "state": "CLOSED",
        },
        {
            "name": 4,
            "file_size_bytes": 2_000 * KB,
            "current": "GLACIER_FLEXIBLE_RETRIEVAL",
            "upload_timestamp": "2026-01-01T00:00:00Z",
            "access_count": 0,
            "state": "ARCHIVED",
        },
        {
            "name": 5,
            "file_size_bytes": 5_000 * KB,
            "current": "GLACIER_DEEP_ARCHIVE",
            "upload_timestamp": "2026-01-01T00:00:00Z",
            "access_count": 30,
            "state": "ARCHIVED",
        },
    ]

    for spec in SCENARIOS:
        document = create_document(
            file_size_bytes=spec["file_size_bytes"],
            current=spec["current"],
            upload_timestamp=spec["upload_timestamp"],
            access_count=spec["access_count"],
            state=spec["state"],
        )

        optimizer_result = optimize_document(document)

        baseline_result = calculate_baseline_cost(
            document,
            REFERENCE_TIMESTAMP,
        )

        optimizer_cost = optimizer_result.recommended_cost
        baseline_cost = baseline_result["total_cost"]

        assert math.isfinite(optimizer_cost)
        assert math.isfinite(baseline_cost)
        assert optimizer_cost > 0.0
        assert baseline_cost > 0.0

        # Policy B: staying is always at least as cheap as moving.
        assert optimizer_result.savings >= 0.0