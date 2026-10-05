from dataclasses import dataclass
from typing import Optional


@dataclass
class OptimizerInput:
    document_id: str
    file_size_bytes: int
    current_storage_class: str
    upload_timestamp: str
    last_accessed_timestamp: Optional[str]
    access_count: int
    days_since_last_access: Optional[float]
    access_frequency: float
    aggregation_timestamp: str
    document_state: Optional[str]


@dataclass
class CostBreakdown:
    storage_cost: float
    retrieval_cost: float
    request_cost: float
    transition_cost: float
    total_cost: float


@dataclass
class TierCost:
    storage_class: str
    cost: CostBreakdown
    retrieval_time_hours: float = 0.0


@dataclass
class OptimizationResult:
    document_id: str
    current_storage_class: str
    recommended_storage_class: str
    eligible_storage_classes: list[str]
    current_cost: float
    recommended_cost: float
    savings: float
    savings_percentage: float
    tier_costs: list[TierCost]
    # State-eligibility policy applied: "A" (state-constrained)
    # or "B" (cost-safe: current class joins the candidate set).
    policy: str = "B"
    # True whenever the current class is not state-eligible —
    # flagged for both policies, whether the engine migrates
    # anyway (Policy A) or keeps the object (Policy B).
    policy_conflict: bool = False
    # Classes the optimizer actually considered (Policy B adds
    # the current class to the state-eligible set).
    candidate_storage_classes: list[str] | None = None
    # Informational: the destination tier's restore wait under
    # the STANDARD retrieval tier. Does not gate selection.
    recommended_retrieval_time_hours: float = 0.0


@dataclass
class WorkloadSummary:
    total_documents: int
    total_storage_gb: float
    current_cost: float
    optimized_cost: float
    total_savings: float
    savings_percentage: float
    transition_count: int
    current_retrieval_cost: float
    optimized_retrieval_cost: float
    current_tier_distribution: dict[str, int]
    recommended_tier_distribution: dict[str, int]
    # Number of documents whose current class is not eligible
    # for their document state (Policy A migrates them anyway;
    # Policy B keeps them when staying is cheaper).
    policy_conflict_count: int = 0