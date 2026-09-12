import json
from collections import defaultdict
from pathlib import Path

from optimization.baseline import calculate_baseline_cost
from optimization.costs import calculate_tier_cost
from optimization.models import OptimizerInput
from optimization.optimizer import optimize_document


WORKLOAD_FILE = Path(
    "data/synthetic/workload_500.json"
)

RESULTS_FILE = Path(
    "data/synthetic/workload_500_results.json"
)


def load_workload() -> dict:
    """Load the generated synthetic workload."""

    with WORKLOAD_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


def create_optimizer_input(
    document: dict,
) -> OptimizerInput:
    """Convert a workload document into optimizer input."""

    return OptimizerInput(
        document_id=document["document_id"],
        file_size_bytes=document["file_size_bytes"],
        current_storage_class=document[
            "current_storage_class"
        ],
        upload_timestamp=document[
            "upload_timestamp"
        ],
        last_accessed_timestamp=document[
            "last_accessed_timestamp"
        ],
        access_count=document["access_count"],
        days_since_last_access=document[
            "days_since_last_access"
        ],
        access_frequency=document[
            "access_frequency"
        ],
        aggregation_timestamp=document[
            "aggregation_timestamp"
        ],
        document_state=document["document_state"],
    )


def calculate_current_cost(
    document: OptimizerInput,
) -> dict:
    """Calculate the cost of keeping the current tier for 12 months."""

    cost = calculate_tier_cost(
        document,
        document.current_storage_class,
        include_transition=False,
    )

    return {
        "storage_cost": cost.storage_cost,
        "retrieval_cost": cost.retrieval_cost,
        "request_cost": cost.request_cost,
        "transition_cost": cost.transition_cost,
        "total_cost": cost.total_cost,
    }


def get_optimizer_cost_breakdown(
    optimizer_result,
) -> dict:
    """Return the cost breakdown for the recommended tier."""

    for tier in optimizer_result.tier_costs:
        if (
            tier.storage_class
            == optimizer_result.recommended_storage_class
        ):
            return {
                "storage_cost": tier.cost.storage_cost,
                "retrieval_cost": tier.cost.retrieval_cost,
                "request_cost": tier.cost.request_cost,
                "transition_cost": tier.cost.transition_cost,
                "total_cost": tier.cost.total_cost,
            }

    raise ValueError(
        "Recommended storage class was not found."
    )


def add_costs(
    first: dict,
    second: dict,
) -> dict:
    """Add two cost breakdown dictionaries."""

    return {
        "storage_cost": (
            first["storage_cost"]
            + second["storage_cost"]
        ),
        "retrieval_cost": (
            first["retrieval_cost"]
            + second["retrieval_cost"]
        ),
        "request_cost": (
            first["request_cost"]
            + second["request_cost"]
        ),
        "transition_cost": (
            first["transition_cost"]
            + second["transition_cost"]
        ),
        "total_cost": (
            first["total_cost"]
            + second["total_cost"]
        ),
    }


def calculate_tier_distribution(
    results: list[dict],
    strategy_key: str,
) -> dict:
    """
    Calculate document count and total storage
    for each storage tier.
    """

    distribution = defaultdict(
        lambda: {
            "document_count": 0,
            "storage_gb": 0.0,
        }
    )

    for result in results:
        storage_class = result[strategy_key]

        file_size_gb = (
            result["file_size_bytes"]
            / 1_000_000_000
        )

        distribution[storage_class][
            "document_count"
        ] += 1

        distribution[storage_class][
            "storage_gb"
        ] += file_size_gb

    return dict(distribution)


def run_experiment() -> None:
    """Run the full 500-document workload experiment."""

    workload_data = load_workload()

    workload = workload_data["documents"]

    reference_timestamp = workload_data[
        "reference_timestamp"
    ]

    detailed_results = []

    total_storage_gb = 0.0

    baseline_costs = {
        "storage_cost": 0.0,
        "retrieval_cost": 0.0,
        "request_cost": 0.0,
        "transition_cost": 0.0,
        "total_cost": 0.0,
    }

    optimizer_costs = {
        "storage_cost": 0.0,
        "retrieval_cost": 0.0,
        "request_cost": 0.0,
        "transition_cost": 0.0,
        "total_cost": 0.0,
    }

    for document in workload:
        optimizer_input = create_optimizer_input(
            document
        )

        optimizer_result = optimize_document(
            optimizer_input
        )

        current_cost = calculate_current_cost(
            optimizer_input
        )

        baseline_result = calculate_baseline_cost(
            optimizer_input,
            reference_timestamp,
        )

        optimizer_cost = get_optimizer_cost_breakdown(
            optimizer_result
        )

        baseline_cost = {
            "storage_cost": baseline_result[
                "storage_cost"
            ],
            "retrieval_cost": baseline_result[
                "retrieval_cost"
            ],
            "request_cost": baseline_result[
                "request_cost"
            ],
            "transition_cost": baseline_result[
                "transition_cost"
            ],
            "total_cost": baseline_result[
                "total_cost"
            ],
        }

        file_size_gb = (
            document["file_size_bytes"]
            / 1_000_000_000
        )

        total_storage_gb += file_size_gb

        baseline_costs = add_costs(
            baseline_costs,
            baseline_cost,
        )

        optimizer_costs = add_costs(
            optimizer_costs,
            optimizer_cost,
        )

        detailed_results.append(
            {
                "document_id": document[
                    "document_id"
                ],
                "file_size_bytes": document[
                    "file_size_bytes"
                ],
                "current_storage_class": document[
                    "current_storage_class"
                ],
                "current_cost": current_cost[
                    "total_cost"
                ],
                "optimizer_storage_class": (
                    optimizer_result
                    .recommended_storage_class
                ),
                "optimizer_cost": (
                    optimizer_result.recommended_cost
                ),
                "optimizer_savings_vs_current": (
                    optimizer_result.savings
                ),
                "optimizer_savings_percentage_vs_current": (
                    optimizer_result.savings_percentage
                ),
                "baseline_storage_class": (
                    baseline_result[
                        "storage_class"
                    ]
                ),
                "baseline_cost": (
                    baseline_result[
                        "total_cost"
                    ]
                ),
                "baseline_transition_count": (
                    baseline_result[
                        "transition_count"
                    ]
                ),
                "optimizer_cost_breakdown": (
                    optimizer_cost
                ),
                "baseline_cost_breakdown": (
                    baseline_cost
                ),
            }
        )

    optimizer_savings = (
        baseline_costs["total_cost"]
        - optimizer_costs["total_cost"]
    )

    if baseline_costs["total_cost"] > 0:
        optimizer_savings_percentage = (
            optimizer_savings
            / baseline_costs["total_cost"]
        ) * 100
    else:
        optimizer_savings_percentage = 0.0

    optimizer_distribution = (
        calculate_tier_distribution(
            detailed_results,
            "optimizer_storage_class",
        )
    )

    baseline_distribution = (
        calculate_tier_distribution(
            detailed_results,
            "baseline_storage_class",
        )
    )

    total_baseline_transitions = sum(
        result["baseline_transition_count"]
        for result in detailed_results
    )

    summary = {
        "total_documents": len(workload),
        "total_storage_gb": total_storage_gb,
        "baseline_cost": baseline_costs[
            "total_cost"
        ],
        "optimizer_cost": optimizer_costs[
            "total_cost"
        ],
        "optimizer_savings_vs_baseline": (
            optimizer_savings
        ),
        "optimizer_savings_percentage_vs_baseline": (
            optimizer_savings_percentage
        ),
        "baseline_tier_distribution": (
            baseline_distribution
        ),
        "optimizer_tier_distribution": (
            optimizer_distribution
        ),
        "baseline_cost_breakdown": baseline_costs,
        "optimizer_cost_breakdown": optimizer_costs,
        "baseline_transition_count": (
            total_baseline_transitions
        ),
    }

    output = {
        "experiment": {
            "workload_file": str(WORKLOAD_FILE),
            "reference_timestamp": (
                reference_timestamp
            ),
            "planning_horizon_months": 12,
        },
        "summary": summary,
        "documents": detailed_results,
    }

    with RESULTS_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            output,
            file,
            indent=2,
        )

    print()
    print("=" * 60)
    print("500-DOCUMENT SYNTHETIC WORKLOAD")
    print("=" * 60)

    print()
    print("WORKLOAD")
    print("-" * 60)
    print(
        f"Documents:                 "
        f"{len(workload)}"
    )
    print(
        f"Storage:                   "
        f"{total_storage_gb:.2f} GB"
    )

    print()
    print("12-MONTH PROJECTED COST")
    print("-" * 60)
    print(
        f"Baseline:                  "
        f"${baseline_costs['total_cost']:.6f}"
    )
    print(
        f"Optimizer:                 "
        f"${optimizer_costs['total_cost']:.6f}"
    )

    print()
    print("OPTIMIZER SAVINGS VS BASELINE")
    print("-" * 60)
    print(
        f"Savings:                   "
        f"${optimizer_savings:.6f}"
    )
    print(
        f"Savings percentage:        "
        f"{optimizer_savings_percentage:.2f}%"
    )

    print()
    print("STORAGE-TIER DISTRIBUTION")
    print("-" * 60)

    print("Baseline:")

    for storage_class in sorted(
        baseline_distribution
    ):
        values = baseline_distribution[
            storage_class
        ]

        print(
            f"  {storage_class:<32}"
            f"{values['document_count']:>4} docs"
            f"  {values['storage_gb']:>10.2f} GB"
        )

    print()
    print("Optimizer:")

    for storage_class in sorted(
        optimizer_distribution
    ):
        values = optimizer_distribution[
            storage_class
        ]

        print(
            f"  {storage_class:<32}"
            f"{values['document_count']:>4} docs"
            f"  {values['storage_gb']:>10.2f} GB"
        )

    print()
    print("COST BREAKDOWN")
    print("-" * 60)

    print("Baseline:")

    print(
        f"  Storage:                  "
        f"${baseline_costs['storage_cost']:.6f}"
    )

    print(
        f"  Retrieval:                "
        f"${baseline_costs['retrieval_cost']:.6f}"
    )

    print(
        f"  Requests:                 "
        f"${baseline_costs['request_cost']:.6f}"
    )

    print(
        f"  Transitions:              "
        f"${baseline_costs['transition_cost']:.6f}"
    )

    print(
        f"  Total:                    "
        f"${baseline_costs['total_cost']:.6f}"
    )

    print()
    print("Optimizer:")

    print(
        f"  Storage:                  "
        f"${optimizer_costs['storage_cost']:.6f}"
    )

    print(
        f"  Retrieval:                "
        f"${optimizer_costs['retrieval_cost']:.6f}"
    )

    print(
        f"  Requests:                 "
        f"${optimizer_costs['request_cost']:.6f}"
    )

    print(
        f"  Transitions:              "
        f"${optimizer_costs['transition_cost']:.6f}"
    )

    print(
        f"  Total:                    "
        f"${optimizer_costs['total_cost']:.6f}"
    )

    print()
    print(
        f"Detailed results saved to: "
        f"{RESULTS_FILE}"
    )


if __name__ == "__main__":
    run_experiment()

