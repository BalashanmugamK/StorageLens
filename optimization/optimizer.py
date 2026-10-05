import math

from .constraints import get_eligible_storage_classes
from .costs import (
    calculate_tier_cost,
    get_retrieval_time_hours_for_classes,
)
from .models import (
    OptimizerInput,
    OptimizationResult,
    TierCost,
)


# Policy A — state-constrained optimization: the document must
# end up in a state-eligible storage class, and among those
# classes the cheapest one is chosen, even when the cheapest
# eligible class costs MORE than keeping the current class.
POLICY_A = "A"

# Policy B — cost-safe recommendation: the current class joins
# the candidate set, so a cost-increasing migration is never
# recommended. When the current class is not state-eligible and
# is also the cheapest option, the object is kept in place and
# the policy conflict is reported separately.
POLICY_B = "B"

DEFAULT_POLICY = POLICY_B


def optimize_document(
    document: OptimizerInput,
    policy: str = DEFAULT_POLICY,
) -> OptimizationResult:
    """Find the lowest-cost eligible storage class for a document."""

    if policy not in (POLICY_A, POLICY_B):
        raise ValueError(
            f"Unsupported optimization policy: {policy} "
            f"(expected {POLICY_A} or {POLICY_B})"
        )

    eligible_classes = get_eligible_storage_classes(
        document.document_state
    )

    # E(state) ∪ {current} under Policy B; E(state) otherwise.
    candidate_classes = list(eligible_classes)

    if (
        policy == POLICY_B
        and document.current_storage_class
        not in candidate_classes
    ):
        candidate_classes.append(
            document.current_storage_class
        )

    tier_costs = []

    retrieval_hours_by_class = (
        get_retrieval_time_hours_for_classes(
            candidate_classes
        )
    )

    for storage_class in candidate_classes:
        cost = calculate_tier_cost(
            document,
            storage_class,
            include_transition=True,
        )

        tier_costs.append(
            TierCost(
                storage_class=storage_class,
                cost=cost,
                retrieval_time_hours=(
                    retrieval_hours_by_class[
                        storage_class
                    ]
                ),
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

    if document.current_storage_class in candidate_classes:
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

    # Policy B never recommends a cost-increasing migration: the
    # current class is already in the candidate set with no
    # transition charge, so a negative number could only come
    # from floating-point noise, and is floored to zero.
    if policy == POLICY_B and savings < 0:
        savings = 0.0

    if current_cost > 0:
        savings_percentage = (
            savings / current_cost
        ) * 100
    else:
        savings_percentage = 0.0

    recommended_retrieval_time_hours = next(
        tier.retrieval_time_hours
        for tier in tier_costs
        if tier.storage_class == recommended_storage_class
    )

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
        policy=policy,
        policy_conflict=(
            document.current_storage_class
            not in eligible_classes
        ),
        candidate_storage_classes=candidate_classes,
        recommended_retrieval_time_hours=(
            recommended_retrieval_time_hours
        ),
    )