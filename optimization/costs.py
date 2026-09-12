import json
from pathlib import Path

from .access import (
    calculate_expected_annual_downloads,
    calculate_expected_retrieved_gb,
)
from .models import CostBreakdown, OptimizerInput


PRICING_FILE = Path(__file__).with_name("pricing.json")


def load_pricing() -> dict:
    """Load the versioned AWS pricing configuration."""
    with PRICING_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


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

    expected_annual_downloads = calculate_expected_annual_downloads(
        document.access_frequency
    )

    expected_retrieved_gb = calculate_expected_retrieved_gb(
        expected_annual_downloads,
        document.file_size_bytes,
    )

    file_size_gb = document.file_size_bytes / 1_000_000_000

    # 12-month storage cost.
    storage_cost = (
        file_size_gb
        * tier["storage_per_gb_month"]
        * horizon_months
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

    # Retrieval/data-retrieval cost.
    retrieval_cost = (
        expected_retrieved_gb
        * tier["retrieval_per_gb"]
    )

    # One-time transition/ingestion cost when moving to another tier.
    transition_cost = 0.0

    if include_transition and storage_class != document.current_storage_class:
        transition_cost = tier["transition_per_1000"] / 1000

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
