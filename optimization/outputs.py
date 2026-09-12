from .models import OptimizationResult, WorkloadSummary


def optimization_result_to_dict(
    result: OptimizationResult,
) -> dict:
    """Convert one optimization result into dashboard-friendly JSON data."""

    tier_costs = []

    for tier in result.tier_costs:
        tier_costs.append(
            {
                "storage_class": tier.storage_class,
                "storage_cost": round(tier.cost.storage_cost, 6),
                "retrieval_cost": round(tier.cost.retrieval_cost, 6),
                "request_cost": round(tier.cost.request_cost, 6),
                "transition_cost": round(tier.cost.transition_cost, 6),
                "total_cost": round(tier.cost.total_cost, 6),
            }
        )

    return {
        "document_id": result.document_id,
        "current_storage_class": result.current_storage_class,
        "recommended_storage_class": result.recommended_storage_class,
        "eligible_storage_classes": result.eligible_storage_classes,
        "current_cost": round(result.current_cost, 6),
        "recommended_cost": round(result.recommended_cost, 6),
        "savings": round(result.savings, 6),
        "savings_percentage": round(result.savings_percentage, 2),
        "tier_costs": tier_costs,
    }


def workload_summary_to_dict(
    summary: WorkloadSummary,
) -> dict:
    """Convert workload metrics into dashboard-friendly JSON data."""

    return {
        "total_documents": summary.total_documents,
        "total_storage_gb": round(summary.total_storage_gb, 6),
        "current_cost": round(summary.current_cost, 6),
        "optimized_cost": round(summary.optimized_cost, 6),
        "total_savings": round(summary.total_savings, 6),
        "savings_percentage": round(summary.savings_percentage, 2),
        "transition_count": summary.transition_count,
        "current_retrieval_cost": round(
            summary.current_retrieval_cost,
            6,
        ),
        "optimized_retrieval_cost": round(
            summary.optimized_retrieval_cost,
            6,
        ),
        "current_tier_distribution": summary.current_tier_distribution,
        "recommended_tier_distribution": (
            summary.recommended_tier_distribution
        ),
    }