from datetime import datetime, timezone

from .access import calculate_expected_annual_downloads
from .costs import (
    calculate_archive_overhead_cost,
    calculate_billable_size_gb,
    calculate_current_tier_early_deletion_fee,
    load_pricing,
)
from .models import OptimizerInput


PLANNING_HORIZON_DAYS = 365
DAYS_PER_MONTH = 365 / 12

LIFECYCLE_RULES = [
    {
        "minimum_age_days": 180,
        "storage_class": "GLACIER_DEEP_ARCHIVE",
    },
    {
        "minimum_age_days": 90,
        "storage_class": "GLACIER_FLEXIBLE_RETRIEVAL",
    },
    {
        "minimum_age_days": 30,
        "storage_class": "STANDARD_IA",
    },
    {
        "minimum_age_days": 0,
        "storage_class": "STANDARD",
    },
]


def parse_timestamp(timestamp: str) -> datetime:
    """Convert an ISO timestamp into a timezone-aware datetime."""

    parsed = datetime.fromisoformat(
        timestamp.replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def calculate_document_age_days(
    upload_timestamp: str,
    reference_timestamp: str,
) -> float:
    """Calculate document age in days."""

    upload_time = parse_timestamp(upload_timestamp)
    reference_time = parse_timestamp(reference_timestamp)

    age = reference_time - upload_time

    return age.total_seconds() / 86400


def select_baseline_storage_class(
    document_age_days: float,
) -> str:
    """Select the lifecycle tier for a given document age."""

    for rule in LIFECYCLE_RULES:
        if document_age_days >= rule["minimum_age_days"]:
            return rule["storage_class"]

    return "STANDARD"


def calculate_lifecycle_periods(
    starting_age_days: float,
    horizon_days: int = PLANNING_HORIZON_DAYS,
) -> list[dict]:
    """
    Calculate the periods spent in each lifecycle tier.

    Each returned period contains:
    - storage_class
    - start_age_days
    - end_age_days
    - duration_days
    """

    periods = []

    current_age = starting_age_days
    remaining_days = float(horizon_days)

    while remaining_days > 0:
        current_class = select_baseline_storage_class(
            current_age
        )

        next_threshold = None

        for rule in reversed(LIFECYCLE_RULES):
            threshold = rule["minimum_age_days"]

            if threshold > current_age:
                next_threshold = threshold
                break

        if next_threshold is None:
            duration_days = remaining_days
        else:
            days_until_transition = (
                next_threshold - current_age
            )

            duration_days = min(
                remaining_days,
                days_until_transition,
            )

        end_age = current_age + duration_days

        periods.append(
            {
                "storage_class": current_class,
                "start_age_days": current_age,
                "end_age_days": end_age,
                "duration_days": duration_days,
            }
        )

        current_age = end_age
        remaining_days -= duration_days

    return periods


def calculate_baseline_cost(
    document: OptimizerInput,
    reference_timestamp: str,
) -> dict:
    """
    Calculate the projected 12-month cost of the fixed
    age-based lifecycle baseline.

    The baseline simulates lifecycle transitions across
    the full 12-month evaluation horizon.

    The baseline is billed exactly like the optimizer's per-tier
    model so the comparison is apples-to-apples: same billable
    object sizes (minimum sizes + archived-object overhead), the
    same recency-attenuated access forecast, and the same
    early-deletion fee for migrating out of the current class
    inside its minimum storage duration.
    """

    pricing = load_pricing()
    classes = pricing["storage_classes"]
    assumptions = pricing["model_assumptions"]

    horizon_days = PLANNING_HORIZON_DAYS

    starting_age_days = calculate_document_age_days(
        document.upload_timestamp,
        reference_timestamp,
    )

    periods = calculate_lifecycle_periods(
        starting_age_days,
        horizon_days,
    )

    recency = assumptions.get("recency_weighting", {})

    # Same forecast the optimizer uses: 30-day frequency
    # extrapolated to a year, attenuated by access recency.
    expected_annual_downloads = (
        calculate_expected_annual_downloads(
            document.access_frequency,
            days_since_last_access=(
                document.days_since_last_access
            ),
            recency_half_life_days=(
                recency.get("half_life_days")
                if recency.get("enabled", False)
                else None
            ),
        )
    )

    total_storage_cost = 0.0
    total_retrieval_cost = 0.0
    total_request_cost = 0.0
    total_transition_cost = 0.0

    period_results = []

    for period in periods:
        storage_class = period["storage_class"]
        tier = classes[storage_class]

        duration_days = period["duration_days"]

        duration_months = (
            duration_days / DAYS_PER_MONTH
        )

        # Billed on the tier's billable size (minimum size +
        # the 32 KB archive-rate metadata overhead), exactly
        # like the optimizer.
        billable_gb = calculate_billable_size_gb(
            tier,
            document.file_size_bytes,
        )

        storage_cost = (
            billable_gb
            * tier["storage_per_gb_month"]
            * duration_months
        ) + calculate_archive_overhead_cost(
            tier,
            duration_months,
            classes["STANDARD"][
                "storage_per_gb_month"
            ],
        )

        retrieval_fraction = (
            duration_days / horizon_days
        )

        period_downloads = (
            expected_annual_downloads
            * retrieval_fraction
        )

        retrieval_cost = (
            period_downloads
            * document.file_size_bytes
            / 1_000_000_000
            * tier["retrieval_per_gb"]
        )

        get_request_cost = (
            period_downloads / 1000
        ) * tier["get_request_per_1000"]

        retrieval_request_cost = (
            period_downloads / 1000
        ) * tier["retrieval_request_per_1000"]

        request_cost = (
            get_request_cost
            + retrieval_request_cost
        )

        total_storage_cost += storage_cost
        total_retrieval_cost += retrieval_cost
        total_request_cost += request_cost

        period_results.append(
            {
                "storage_class": storage_class,
                "start_age_days": period["start_age_days"],
                "end_age_days": period["end_age_days"],
                "duration_days": duration_days,
                "storage_cost": storage_cost,
                "retrieval_cost": retrieval_cost,
                "request_cost": request_cost,
                "download_count": period_downloads,
            }
        )

    transition_count = 0

    first_baseline_class = periods[0]["storage_class"]

    if document.current_storage_class != first_baseline_class:
        transition_count += 1

        destination_tier = classes[first_baseline_class]

        # The initial migration out of the current class carries
        # the same prorated early-deletion fee the optimizer
        # charges for it (leaving a class inside its minimum
        # storage duration is paid regardless of destination).
        total_transition_cost += (
            destination_tier["transition_per_1000"]
            / 1000
            + calculate_current_tier_early_deletion_fee(
                document,
                pricing,
            )
        )

    for index in range(1, len(periods)):
        previous_class = periods[index - 1]["storage_class"]
        current_class = periods[index]["storage_class"]

        if previous_class != current_class:
            transition_count += 1

            destination_tier = classes[current_class]

            total_transition_cost += (
                destination_tier["transition_per_1000"]
                / 1000
            )

    total_cost = (
        total_storage_cost
        + total_retrieval_cost
        + total_request_cost
        + total_transition_cost
    )

    return {
        "storage_class": periods[0]["storage_class"],
        "age_days": starting_age_days,
        "periods": period_results,
        "transition_count": transition_count,
        "storage_cost": total_storage_cost,
        "retrieval_cost": total_retrieval_cost,
        "request_cost": total_request_cost,
        "transition_cost": total_transition_cost,
        "total_cost": total_cost,
    }
