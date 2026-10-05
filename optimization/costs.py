import json
import warnings
from datetime import datetime, timezone
from pathlib import Path

from .access import (
    calculate_expected_annual_downloads,
    calculate_expected_retrieved_gb,
)
from .models import CostBreakdown, OptimizerInput


PRICING_FILE = Path(__file__).with_name("pricing.json")

SECONDS_PER_DAY = 86_400

# The archived-object overhead consists of 8 KB billed at S3
# Standard rates and 32 KB billed at the archive's own rate.
_BYTES_PER_GB = 1_000_000_000


def load_pricing() -> dict:
    """Load the versioned AWS pricing configuration."""

    with PRICING_FILE.open("r", encoding="utf-8") as file:
        pricing = json.load(file)

    warn_if_pricing_stale(pricing)

    return pricing


_STALENESS_WARNED = False


def warn_if_pricing_stale(pricing: dict) -> None:
    """
    Warn once per process when the pricing snapshot is older
    than model_assumptions.pricing_max_age_days.
    """

    global _STALENESS_WARNED

    if _STALENESS_WARNED:
        return

    assumptions = pricing.get("model_assumptions", {})

    max_age_days = assumptions.get("pricing_max_age_days")

    checked = pricing.get("pricing_checked")

    if max_age_days is None or not checked:
        return

    try:
        checked_date = datetime.strptime(
            str(checked), "%Y-%m-%d"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return

    age_days = (
        datetime.now(timezone.utc) - checked_date
    ).days

    if age_days > max_age_days:
        _STALENESS_WARNED = True

        warnings.warn(
            "pricing.json snapshot (checked "
            f"{checked}) is {age_days} days old, which exceeds "
            f"the freshness budget of {max_age_days} days. "
            "Refresh it with scripts/refresh_pricing.py so "
            "tier recommendations track current AWS rates.",
            stacklevel=3,
        )


def calculate_billable_size_gb(
    tier: dict,
    file_size_bytes: int,
) -> float:
    """
    Billable storage size in GB for one object in one tier.

    Standard and Standard-IA / Glacier Instant Retrieval bill on
    a minimum object size (128 KB for the IA-class tiers), and
    Glacier Flexible Retrieval / Deep Archive add the archived
    metadata overhead (32 KB at the archive rate + 8 KB at
    Standard rates; the 8 KB part is billed separately by the
    caller via calculate_archive_overhead_cost).
    """

    min_bytes = tier.get("min_billable_object_bytes", 0)

    billable_bytes = max(
        int(file_size_bytes),
        int(min_bytes),
    )

    overhead_bytes = tier.get(
        "archive_overhead_bytes_at_glacier_rate", 0
    )

    return (
        billable_bytes + overhead_bytes
    ) / _BYTES_PER_GB


def calculate_archive_overhead_cost(
    tier: dict,
    horizon_months: int,
    standard_rate_per_gb_month: float,
) -> float:
    """
    The 8 KB metadata portion of the archived-object overhead,
    billed at S3 Standard rates for the horizon. The 32 KB
    archive-rate portion is already inside the billable size.
    """

    standard_bytes = tier.get(
        "archive_overhead_bytes_at_standard_rate", 0
    )

    if not standard_bytes:
        return 0.0

    return (
        (standard_bytes / _BYTES_PER_GB)
        * standard_rate_per_gb_month
        * horizon_months
    )


def calculate_days_held(document: OptimizerInput) -> float:
    """
    Age of the object in its current class, in days.

    Derived from the aggregate's upload and aggregation
    timestamps so the calculation is deterministic and
    unit-testable (a live re-run would use the same pair).
    """

    held_since = datetime.fromisoformat(
        str(document.upload_timestamp).replace("Z", "+00:00")
    )

    reference = datetime.fromisoformat(
        str(document.aggregation_timestamp).replace(
            "Z", "+00:00"
        )
    )

    held_seconds = (
        reference - held_since
    ).total_seconds()

    return max(0.0, held_seconds / SECONDS_PER_DAY)


def calculate_current_tier_early_deletion_fee(
    document: OptimizerInput,
    pricing: dict,
) -> float:
    """
    Prorated minimum-storage-duration fee for leaving the
    CURRENT class too early.

    S3 charges the remaining minimum-duration days at the
    current class's storage rate (SIA 30 days, Flexible/
    Instant Retrieval 90, Deep Archive 180). The fee applies
    only when the object is actually migrated away.
    """

    classes = pricing["storage_classes"]

    current_tier = classes.get(
        document.current_storage_class
    )

    if current_tier is None:
        return 0.0

    minimum_days = current_tier.get(
        "minimum_storage_days", 0
    )

    if not minimum_days or minimum_days <= 0:
        return 0.0

    held_days = calculate_days_held(document)

    remaining_days = minimum_days - held_days

    if remaining_days <= 0:
        return 0.0

    daily_rate = (
        current_tier["storage_per_gb_month"] / 30
    )

    billable_gb = calculate_billable_size_gb(
        current_tier,
        document.file_size_bytes,
    )

    return (
        remaining_days
        * daily_rate
        * billable_gb
    )


def get_retrieval_time_hours(storage_class: str) -> float:
    """
    Informational restore wait for one tier under the STANDARD
    retrieval tier (0 for online classes). Selection never
    gates on this value; it is surfaced for reporting.
    """

    classes = load_pricing()["storage_classes"]

    return float(
        classes[storage_class].get(
            "retrieval_time_hours", 0
        )
    )


def get_retrieval_time_hours_for_classes(
    storage_classes: list[str],
) -> dict[str, float]:
    """Restore-wait lookup for several classes in one read."""

    classes = load_pricing()["storage_classes"]

    return {
        storage_class: float(
            classes[storage_class].get(
                "retrieval_time_hours", 0
            )
        )
        for storage_class in storage_classes
    }


def calculate_tier_cost(
    document: OptimizerInput,
    storage_class: str,
    include_transition: bool = True,
) -> CostBreakdown:
    """Calculate the projected 12-month cost for one storage class."""

    pricing = load_pricing()
    classes = pricing["storage_classes"]

    if storage_class not in classes:
        raise ValueError(
            f"Unsupported storage class: {storage_class}"
        )

    tier = classes[storage_class]
    assumptions = pricing["model_assumptions"]

    horizon_months = assumptions["planning_horizon_months"]

    recency = assumptions.get("recency_weighting", {})

    expected_annual_downloads = calculate_expected_annual_downloads(
        document.access_frequency,
        days_since_last_access=document.days_since_last_access,
        recency_half_life_days=(
            recency.get("half_life_days")
            if recency.get("enabled", False)
            else None
        ),
    )

    expected_retrieved_gb = calculate_expected_retrieved_gb(
        expected_annual_downloads,
        document.file_size_bytes,
    )

    # 12-month storage cost, on the tier's billable size
    # (minimum object size + archive metadata overhead).
    storage_cost = (
        calculate_billable_size_gb(
            tier,
            document.file_size_bytes,
        )
        * tier["storage_per_gb_month"]
        * horizon_months
    ) + calculate_archive_overhead_cost(
        tier,
        horizon_months,
        classes["STANDARD"]["storage_per_gb_month"],
    )

    # Normal GET request cost.
    get_request_cost = (
        expected_annual_downloads / 1000
    ) * tier["get_request_per_1000"]

    # Archive classes additionally have a retrieval/restore request charge.
    retrieval_request_cost = (
        expected_annual_downloads / 1000
    ) * tier["retrieval_request_per_1000"]

    request_cost = get_request_cost + retrieval_request_cost

    # Retrieval/data-retrieval cost, billed on the actual bytes
    # retrieved, not on the billable size.
    retrieval_cost = (
        expected_retrieved_gb
        * tier["retrieval_per_gb"]
    )

    # One-time migration costs when moving to another tier: the
    # destination's lifecycle transition request plus a prorated
    # early-deletion fee if the object leaves its current class
    # inside the current class's minimum storage duration.
    transition_cost = 0.0

    if (
        include_transition
        and storage_class != document.current_storage_class
    ):
        transition_cost = (
            tier["transition_per_1000"] / 1000
        ) + calculate_current_tier_early_deletion_fee(
            document,
            pricing,
        )

    total_cost = (
        storage_cost
        + retrieval_cost
        + request_cost
        + transition_cost
    )

    return CostBreakdown(
        storage_cost=storage_cost,
        retrieval_cost=retrieval_cost,
        request_cost=request_cost,
        transition_cost=transition_cost,
        total_cost=total_cost,
    )