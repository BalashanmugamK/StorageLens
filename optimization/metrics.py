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

    current_tier_distribution = {}
    recommended_tier_distribution = {}

    for result in results:
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

        for tier in result.tier_costs:
            if tier.storage_class == result.current_storage_class:
                current_retrieval_cost += (
                    tier.cost.retrieval_cost
                )

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
        recommended_tier_distribution=recommended_tier_distribution,
    )