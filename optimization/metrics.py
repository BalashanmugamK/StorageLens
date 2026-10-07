from .costs import calculate_tier_cost
from .models import OptimizerInput, OptimizationResult, WorkloadSummary


def calculate_workload_summary(
    documents: list[OptimizerInput],
    results: list[OptimizationResult],
) -> WorkloadSummary:
    """Calculate aggregate metrics for an optimized workload."""

    if len(documents) != len(results):
        raise ValueError(
            "documents and results must contain the same number of items"
        )

    total_documents = len(documents)

    total_storage_bytes = 0

    for document in documents:
        total_storage_bytes += document.file_size_bytes

    total_storage_gb = total_storage_bytes / 1_000_000_000

    current_cost = 0.0
    optimized_cost = 0.0
    current_retrieval_cost = 0.0
    optimized_retrieval_cost = 0.0

    transition_count = 0
    policy_conflict_count = 0

    current_tier_distribution = {}
    recommended_tier_distribution = {}

    for document, result in zip(documents, results):
        current_cost += result.current_cost
        optimized_cost += result.recommended_cost

        current_tier_distribution[
            result.current_storage_class
        ] = (
            current_tier_distribution.get(
                result.current_storage_class,
                0,
            )
            + 1
        )

        recommended_tier_distribution[
            result.recommended_storage_class
        ] = (
            recommended_tier_distribution.get(
                result.recommended_storage_class,
                0,
            )
            + 1
        )

        if (
            result.current_storage_class
            != result.recommended_storage_class
        ):
            transition_count += 1

        if getattr(result, "policy_conflict", False):
            policy_conflict_count += 1

        # Under Policy A the current class is not state-eligible
        # and may be missing from tier_costs entirely, so the
        # current-class economics are re-derived from the cost
        # model instead of silently reporting 0.
        current_breakdown = next(
            (
                tier.cost
                for tier in result.tier_costs
                if tier.storage_class
                == result.current_storage_class
            ),
            calculate_tier_cost(
                document,
                result.current_storage_class,
                include_transition=False,
            ),
        )

        current_retrieval_cost += (
            current_breakdown.retrieval_cost
        )

        for tier in result.tier_costs:
            if tier.storage_class == result.recommended_storage_class:
                optimized_retrieval_cost += (
                    tier.cost.retrieval_cost
                )

    total_savings = current_cost - optimized_cost

    if current_cost > 0:
        savings_percentage = (
            total_savings / current_cost
        ) * 100
    else:
        savings_percentage = 0.0

    return WorkloadSummary(
        total_documents=total_documents,
        total_storage_gb=total_storage_gb,
        current_cost=current_cost,
        optimized_cost=optimized_cost,
        total_savings=total_savings,
        savings_percentage=savings_percentage,
        transition_count=transition_count,
        current_retrieval_cost=current_retrieval_cost,
        optimized_retrieval_cost=optimized_retrieval_cost,
        current_tier_distribution=current_tier_distribution,
        recommended_tier_distribution=(
            recommended_tier_distribution
        ),
        policy_conflict_count=policy_conflict_count,
    )