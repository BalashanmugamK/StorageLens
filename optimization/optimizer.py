import math

from .constraints import get_eligible_storage_classes
from .costs import calculate_tier_cost
from .models import (
    OptimizerInput,
    OptimizationResult,
    TierCost,
)


def optimize_document(document: OptimizerInput) -> OptimizationResult:
    """Find the lowest-cost eligible storage class for a document."""

    eligible_classes = get_eligible_storage_classes(
        document.document_state
    )

    tier_costs = []

    for storage_class in eligible_classes:
        cost = calculate_tier_cost(
            document,
            storage_class,
            include_transition=True,
        )

        tier_costs.append(
            TierCost(
                storage_class=storage_class,
                cost=cost,
            )
        )

    current_cost_breakdown = calculate_tier_cost(
        document,
        document.current_storage_class,
        include_transition=False,
    )

    current_cost = current_cost_breakdown.total_cost

    minimum_cost = min(
        tier.cost.total_cost
        for tier in tier_costs
    )

    recommended_storage_class = None

    if document.current_storage_class in eligible_classes:
        current_candidate_cost = next(
            tier.cost.total_cost
            for tier in tier_costs
            if tier.storage_class == document.current_storage_class
        )

        if math.isclose(
            current_candidate_cost,
            minimum_cost,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            recommended_storage_class = document.current_storage_class

    if recommended_storage_class is None:
        recommended_storage_class = min(
            tier_costs,
            key=lambda tier: tier.cost.total_cost,
        ).storage_class

    recommended_cost = next(
        tier.cost.total_cost
        for tier in tier_costs
        if tier.storage_class == recommended_storage_class
    )

    savings = current_cost - recommended_cost

    if current_cost > 0:
        savings_percentage = (
            savings / current_cost
        ) * 100
    else:
        savings_percentage = 0.0

    return OptimizationResult(
        document_id=document.document_id,
        current_storage_class=document.current_storage_class,
        recommended_storage_class=recommended_storage_class,
        eligible_storage_classes=eligible_classes,
        current_cost=current_cost,
        recommended_cost=recommended_cost,
        savings=savings,
        savings_percentage=savings_percentage,
        tier_costs=tier_costs,
    )