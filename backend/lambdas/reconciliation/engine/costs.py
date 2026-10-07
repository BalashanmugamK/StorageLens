import json
import math
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


# Intelligent-Tiering moves an object to the Infrequent tier
# after 30 consecutive access-free days and to Archive-Instant
# after 90; its archive and deep-archive tiers are optional
# per-bucket opt-ins and are NOT modeled.
IT_TIER_ACTIVATION_COLD_DAYS = {
    "infrequent": 30,
    "archive_instant": 90,
}


def calculate_intelligent_tiering_blend(
    expected_annual_downloads: float,
) -> dict[str, float]:
    """
    Expected fraction of a 365-day horizon spent in each
    Intelligent-Tiering layer.

    Access is modeled as a homogeneous Poisson process with rate
    lambda = expected annual downloads. In steady state the
    probability that "the last access is more than d days ago"
    is exp(-lambda * d / 365):

        archive_instant fraction = P(cold >= 90 days)
        infrequent fraction      = P(cold >= 30 days) - that
        frequent fraction        = the rest

    This matches the observed 30/90-day activation thresholds and
    keeps the blend explainable; bursty workloads will spend MORE
    time in Frequent than this model predicts, so the blend is
    optimistic about IT savings for lumpy access patterns.
    """

    if expected_annual_downloads <= 0:
        # Never accessed again: 30 days of Frequent while the
        # object cools (its last tiering reset was now), then
        # Infrequent, then Archive-Instant.
        archive_instant = (
            365 - IT_TIER_ACTIVATION_COLD_DAYS["archive_instant"]
        ) / 365
        infrequent = (
            IT_TIER_ACTIVATION_COLD_DAYS["archive_instant"]
            - IT_TIER_ACTIVATION_COLD_DAYS["infrequent"]
        ) / 365
        frequent = IT_TIER_ACTIVATION_COLD_DAYS["infrequent"] / 365

        return {
            "frequent": frequent,
            "infrequent": infrequent,
            "archive_instant": archive_instant,
        }

    days_per_year = 365.0

    cold_90 = math.exp(
        -expected_annual_downloads
        * IT_TIER_ACTIVATION_COLD_DAYS["archive_instant"]
        / days_per_year
    )

    cold_30 = math.exp(
        -expected_annual_downloads
        * IT_TIER_ACTIVATION_COLD_DAYS["infrequent"]
        / days_per_year
    )

    return {
        "frequent": 1.0 - cold_30,
        "infrequent": cold_30 - cold_90,
        "archive_instant": cold_90,
    }


def calculate_intelligent_tiering_costs(
    tier: dict,
    document: OptimizerInput,
    expected_annual_downloads: float,
    horizon_months: int,
) -> tuple[float, float]:
    """
    Storage and monitoring cost for one object in
    Intelligent-Tiering over the horizon.

    Returns (storage_cost, monitoring_fee). Billable storage is
    the real object size (IT bills per GB with no per-object
    minimum); a monitoring/automation fee applies per object-
    month to monitored objects. Objects smaller than the tiering
    minimum (128 KB) live the whole year in the Frequent layer,
    are never monitored (no fee), and never tier down.
    """

    min_bytes = int(
        tier.get("min_billable_object_bytes", 0)
    )

    monitored = document.file_size_bytes >= min_bytes

    if not monitored:
        # Unmonitored object: Frequent layer all horizon.
        billable_gb = (
            document.file_size_bytes / _BYTES_PER_GB
        )

        storage_cost = (
            tier["storage_per_gb_month_frequent"]
            * billable_gb
            * horizon_months
        )

        return storage_cost, 0.0

    blend = calculate_intelligent_tiering_blend(
        expected_annual_downloads
    )

    billable_gb = (
        document.file_size_bytes / _BYTES_PER_GB
    )

    blended_rate = (
        blend["frequent"]
        * tier["storage_per_gb_month_frequent"]
        + blend["infrequent"]
        * tier["storage_per_gb_month_infrequent"]
        + blend["archive_instant"]
        * tier["storage_per_gb_month_archive_instant"]
    )

    storage_cost = (
        blended_rate * billable_gb * horizon_months
    )

    monitoring_fee = (
        tier[
            "monitoring_automation_fee_per_1000_object_month"
        ]
        / 1000
    ) * horizon_months

    return storage_cost, monitoring_fee


def calculate_days_held(document: OptimizerInput) -> float:
    """
    Days the object has been held in its CURRENT class.

    AWS's minimum-storage-duration clock starts when the object
    TRANSITIONS INTO a class. When the pipeline has observed a
    transition (current_class_since), anchor there; otherwise
    fall back to the upload timestamp, which is exact for
    directly uploaded objects.

    Derived from timestamps against the aggregation timestamp so
    the calculation is deterministic and unit-testable (a live
    re-run would use the same pair).
    """

    held_since = datetime.fromisoformat(
        str(
            document.current_class_since
            or document.upload_timestamp
        ).replace("Z", "+00:00")
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

    # 12-month storage cost. Intelligent-Tiering has its own
    # multi-layer model (Poisson blend over FA/IA/AIA + a per-
    # object-month monitoring fee); every other class bills a
    # single rate on the tier's billable size (minimum object
    # size + archive metadata overhead).
    if "storage_per_gb_month_frequent" in tier:
        storage_cost, monitoring_fee = (
            calculate_intelligent_tiering_costs(
                tier,
                document,
                expected_annual_downloads,
                horizon_months,
            )
        )
    else:
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

        monitoring_fee = 0.0

    # Normal GET request cost.
    get_request_cost = (
        expected_annual_downloads / 1000
    ) * tier["get_request_per_1000"]

    # Archive classes additionally have a retrieval/restore request charge.
    retrieval_request_cost = (
        expected_annual_downloads / 1000
    ) * tier["retrieval_request_per_1000"]

    # IT's monitoring/automation fee is an operational per-object
    # charge, so it is grouped with the request costs.
    request_cost = (
        get_request_cost
        + retrieval_request_cost
        + monitoring_fee
    )

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