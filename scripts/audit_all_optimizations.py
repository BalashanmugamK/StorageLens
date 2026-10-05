"""
Independent audit of every record in the live DocumentAggregatesTable.

This is an AUDIT: nothing under optimization/ is modified. The audit
does not trust the optimizer's own arithmetic. It re-derives each
tier's 12-month cost independently from the SAME existing building
blocks the repository uses:

  - pricing inputs:  optimization.costs.load_pricing()  (pricing.json)
  - access scaling:  optimization.access expected-download formulas
  - eligibility:     optimization.constraints.get_eligible_storage_classes

and only then compares the independently computed cheapest tier
against what the untouched optimize_document() recommended.

Checks per record:
  recommendation_check  recommended tier attains the independently
                        computed minimum cost among candidate tiers
                        (ties allowed, same as the optimizer)
  cost_check            optimizer's recommended_cost equals the
                        independent cost of the recommended tier
  current_cost_check    optimizer's current_cost equals the
                        independent cost of the current tier
  savings_check         savings == current_cost - recommended_cost
                        (floored at zero under policy B)
  conflict_check        policy_conflict flag matches whether the
                        current class is outside the state-eligible set

Usage:
    python scripts/audit_all_optimizations.py
        [--stack-name intelligent-storage-cost-optimizer]
        [--region ap-south-1]
        [--policy A|B]

Exits 0 only when every record passes.
"""

import argparse
import csv
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT))

from load_synthetic_data import resolve_stack_resources

from optimization.access import (
    calculate_expected_annual_downloads,
    calculate_expected_retrieved_gb,
)
from optimization.adapters import from_aggregate_item
from optimization.constraints import get_eligible_storage_classes
from optimization.costs import load_pricing
from optimization.optimizer import optimize_document


CSV_FILE = (
    REPO_ROOT / "experiments" / "results"
    / "audit_all_optimizations.csv"
)

# Float comparisons on recomputed costs need small tolerances to
# absorb reordering of identical arithmetic operations.
COST_TOLERANCE = 1e-6
SAVINGS_TOLERANCE = 1e-6

GB = 1_000_000_000


def scan_all_items(table):
    """Scan every DynamoDB pagination page."""

    items = []
    scan_kwargs = {}

    while True:
        result = table.scan(**scan_kwargs)

        items.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_kwargs["ExclusiveStartKey"] = last_key

    return items


def independent_recency_weight(document, pricing):
    """
    Mirror of the recency attenuation applied to the annual
    download forecast, recomposed here from pricing.json only.
    """

    recency = pricing["model_assumptions"].get(
        "recency_weighting", {}
    )

    if not recency.get("enabled", False):
        return 1.0

    return calculate_expected_annual_downloads(
        1.0,
        days_since_last_access=document.days_since_last_access,
        recency_half_life_days=recency.get("half_life_days"),
    ) / 365.0


def independent_billable_gb(tier, document):
    """Mirror of minimum-object-size + archive-overhead billing."""

    min_bytes = tier.get("min_billable_object_bytes", 0)

    billable_bytes = max(
        document.file_size_bytes,
        min_bytes,
    )

    overhead_bytes = tier.get(
        "archive_overhead_bytes_at_glacier_rate", 0
    )

    return (billable_bytes + overhead_bytes) / GB


def independent_current_tier_early_deletion_fee(
    document, pricing
):
    """
    Mirror of the prorated minimum-storage-duration fee the
    current class charges when the object leaves early.
    """

    tier = pricing["storage_classes"].get(
        document.current_storage_class
    )

    if tier is None:
        return 0.0

    minimum_days = tier.get("minimum_storage_days", 0)

    if not minimum_days or minimum_days <= 0:
        return 0.0

    held_since = datetime.fromisoformat(
        str(document.upload_timestamp).replace("Z", "+00:00")
    )

    reference = datetime.fromisoformat(
        str(document.aggregation_timestamp).replace(
            "Z", "+00:00"
        )
    )

    held_days = max(
        0.0,
        (
            reference - held_since
        ).total_seconds() / 86400,
    )

    remaining_days = minimum_days - held_days

    if remaining_days <= 0:
        return 0.0

    daily_rate = tier["storage_per_gb_month"] / 30

    return (
        remaining_days
        * daily_rate
        * independent_billable_gb(tier, document)
    )


def independent_cost_per_gb_model(
    document,
    storage_class,
    pricing,
    include_transition=True,
):
    """
    Independently evaluate the 12-month cost of one storage class
    using the same cost inputs and fidelity rules as the repo's
    current cost model (pricing.json + access.py), recomposed here
    rather than calling optimization.costs.calculate_tier_cost:

      - recency-attenuated annual download forecast
      - minimum billable object size per class
      - archived-object overhead (32 KB archive rate + 8 KB
        S3 Standard rate)
      - prorated early-deletion fee on the current class
    """

    tier = pricing["storage_classes"][storage_class]

    assumptions = pricing["model_assumptions"]

    horizon_months = assumptions["planning_horizon_months"]

    recency = assumptions.get("recency_weighting", {})

    expected_annual_downloads = (
        calculate_expected_annual_downloads(
            document.access_frequency,
            days_since_last_access=document.days_since_last_access,
            recency_half_life_days=(
                recency.get("half_life_days")
                if recency.get("enabled", False)
                else None
            ),
        )
    )

    expected_retrieved_gb = (
        calculate_expected_retrieved_gb(
            expected_annual_downloads,
            document.file_size_bytes,
        )
    )

    standard_rate = pricing["storage_classes"][
        "STANDARD"
    ]["storage_per_gb_month"]

    # Storage on the billable size, plus the 8 KB Standard-rate
    # metadata portion for archive classes.
    storage_cost = (
        independent_billable_gb(tier, document)
        * tier["storage_per_gb_month"]
        * horizon_months
    )

    standard_overhead_bytes = tier.get(
        "archive_overhead_bytes_at_standard_rate", 0
    )

    storage_cost += (
        (standard_overhead_bytes / GB)
        * standard_rate
        * horizon_months
    )

    get_request_cost = (
        expected_annual_downloads / 1000
    ) * tier["get_request_per_1000"]

    retrieval_request_cost = (
        expected_annual_downloads / 1000
    ) * tier["retrieval_request_per_1000"]

    retrieval_cost = (
        expected_retrieved_gb
        * tier["retrieval_per_gb"]
    )

    transition_cost = 0.0

    if (
        include_transition
        and storage_class != document.current_storage_class
    ):
        transition_cost = (
            tier["transition_per_1000"] / 1000
            + independent_current_tier_early_deletion_fee(
                document, pricing
            )
        )

    total_cost = (
        storage_cost
        + retrieval_cost
        + request_total(get_request_cost, retrieval_request_cost)
        + transition_cost
    )

    return {
        "storage_cost": storage_cost,
        "retrieval_cost": retrieval_cost,
        "request_cost": (
            get_request_cost + retrieval_request_cost
        ),
        "transition_cost": transition_cost,
        "total_cost": total_cost,
    }


def request_total(get_cost, retrieval_cost):
    return get_cost + retrieval_cost


def classify(savings):
    if abs(savings) <= SAVINGS_TOLERANCE:
        return "NO CHANGE"
    if savings > 0:
        return "POSITIVE SAVINGS"
    return "NEGATIVE SAVINGS"


def savings_gte_zero(savings, policy):
    """Under policy B a negative savings recommendation never exists."""

    if policy != "B":
        return True

    return savings >= -SAVINGS_TOLERANCE


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Independently audit every live aggregate record "
            "through the repository optimizer."
        )
    )

    parser.add_argument(
        "--stack-name",
        default="intelligent-storage-cost-optimizer",
    )

    parser.add_argument(
        "--region",
        default="ap-south-1",
    )

    parser.add_argument(
        "--policy",
        choices=["A", "B"],
        default="B",
        help=(
            "Eligibility policy to audit; must match the "
            "optimizer's policy (default B)."
        ),
    )

    args = parser.parse_args()

    import boto3

    try:
        resources = resolve_stack_resources(
            args.stack_name,
            args.region,
        )
    except Exception as error:
        print(
            f"ERROR: could not resolve deployed stack "
            f"resources: {error}",
            file=sys.stderr,
        )
        return 4

    aggregates_table_name = resources["aggregates_table"]

    if not aggregates_table_name:
        print(
            "ERROR: DocumentAggregatesTable output not found "
            "in stack.",
            file=sys.stderr,
        )
        return 4

    pricing = load_pricing()

    print(f"Stack:    {args.stack_name}")
    print(f"Region:   {args.region}")
    print(f"Table:    {aggregates_table_name}")
    print("Scanning all aggregate records ...")

    dynamodb = boto3.resource(
        "dynamodb", region_name=args.region
    )

    aggregates_table = dynamodb.Table(aggregates_table_name)

    items = scan_all_items(aggregates_table)

    rows = []
    fail_details = []

    pass_count = 0
    fail_count = 0

    recommendation_mismatches = 0
    cost_mismatches = 0
    current_cost_mismatches = 0
    savings_mismatches = 0
    conflict_mismatches = 0

    recommended_counts = Counter()
    state_counts = Counter()

    for item in items:
        document_id = item.get("document_id", "<missing-id>")

        document_state = item.get("document_state")

        try:
            document = from_aggregate_item(item)
            result = optimize_document(
                document,
                policy=args.policy,
            )
        except Exception as error:
            fail_count += 1
            fail_details.append(
                {
                    "document_id": document_id,
                    "error": (
                        f"{type(error).__name__}: {error}"
                    ),
                }
            )
            rows.append(
                {
                    "document_id": document_id,
                    "document_state": document_state or "",
                    "recommendation_check": "FAIL",
                    "cost_check": "FAIL",
                    "savings_check": "FAIL",
                }
            )
            continue

        # Independent recomputation over repository rules only.
        eligible = get_eligible_storage_classes(
            document.document_state
        )

        candidates = list(eligible)

        if (
            args.policy == "B"
            and document.current_storage_class not in candidates
        ):
            candidates.append(document.current_storage_class)

        independent_costs = {
            storage_class: independent_cost_per_gb_model(
                document, storage_class, pricing
            )
            for storage_class in candidates
        }

        independent_current_cost = (
            independent_cost_per_gb_model(
                document,
                document.current_storage_class,
                pricing,
                include_transition=False,
            )["total_cost"]
        )

        independent_cheapest_class = min(
            independent_costs,
            key=lambda storage_class: independent_costs[
                storage_class
            ]["total_cost"],
        )

        independent_cheapest_cost = independent_costs[
            independent_cheapest_class
        ]["total_cost"]

        recommended_cost_independent = (
            independent_costs[
                result.recommended_storage_class
            ]["total_cost"]
            if result.recommended_storage_class
            in independent_costs
            else float("inf")
        )

        recommendation_ok = math.isclose(
            recommended_cost_independent,
            independent_cheapest_cost,
            rel_tol=1e-9,
            abs_tol=COST_TOLERANCE,
        )

        cost_ok = math.isclose(
            result.recommended_cost,
            recommended_cost_independent,
            rel_tol=1e-9,
            abs_tol=COST_TOLERANCE,
        ) if recommended_cost_independent != float("inf") else False

        current_cost_ok = math.isclose(
            result.current_cost,
            independent_current_cost,
            rel_tol=1e-9,
            abs_tol=COST_TOLERANCE,
        )

        savings_ok = math.isclose(
            result.savings,
            result.current_cost - result.recommended_cost,
            rel_tol=0.0,
            abs_tol=SAVINGS_TOLERANCE,
        ) and savings_gte_zero(
            result.savings, args.policy
        )

        conflict_ok = getattr(
            result, "policy_conflict", None
        ) == (
            document.current_storage_class not in eligible
        )

        passed = (
            recommendation_ok
            and cost_ok
            and current_cost_ok
            and savings_ok
            and conflict_ok
        )

        if passed:
            pass_count += 1
        else:
            fail_count += 1

            fail_details.append(
                {
                    "document_id": document_id,
                    "optimizer_recommended": (
                        result.recommended_storage_class
                    ),
                    "optimizer_recommended_cost": (
                        result.recommended_cost
                    ),
                    "optimizer_current_cost": (
                        result.current_cost
                    ),
                    "optimizer_savings": result.savings,
                    "independent_cheapest": (
                        independent_cheapest_class
                    ),
                    "independent_cheapest_cost": (
                        independent_cheapest_cost
                    ),
                    "independent_recommended_cost": (
                        recommended_cost_independent
                    ),
                    "recommendation_check": (
                        "PASS" if recommendation_ok else "FAIL"
                    ),
                    "cost_check": "PASS" if cost_ok else "FAIL",
                    "savings_check": (
                        "PASS" if savings_ok else "FAIL"
                    ),
                    "eligible": eligible,
                    "candidates": candidates,
                    "current_cost_check": (
                        "PASS" if current_cost_ok else "FAIL"
                    ),
                    "independent_current_cost": (
                        independent_current_cost
                    ),
                    "conflict_check": (
                        "PASS" if conflict_ok else "FAIL"
                    ),
                    "row": {
                        k: v
                        for k, v in independent_costs.items()
                    },
                }
            )

        recommendation_mismatches += 0 if recommendation_ok else 1
        cost_mismatches += 0 if cost_ok else 1
        current_cost_mismatches += 0 if current_cost_ok else 1
        savings_mismatches += 0 if savings_ok else 1
        conflict_mismatches += 0 if conflict_ok else 1

        recommended_counts[
            result.recommended_storage_class
        ] += 1
        state_counts[
            document.document_state or "NULL"
        ] += 1

        rows.append(
            {
                "document_id": document.document_id,
                "document_state": document.document_state or "",
                "file_size_bytes": document.file_size_bytes,
                "current_storage_class": (
                    document.current_storage_class
                ),
                "access_count": document.access_count,
                "access_frequency": (
                    f"{document.access_frequency:.12f}"
                ),
                "days_since_last_access": (
                    document.days_since_last_access
                    if document.days_since_last_access is not None
                    else ""
                ),
                "recommended_storage_class": (
                    result.recommended_storage_class
                ),
                "current_annual_cost": (
                    f"{result.current_cost:.8f}"
                ),
                "recommended_annual_cost": (
                    f"{result.recommended_cost:.8f}"
                ),
                "savings": f"{result.savings:.8f}",
                "policy": result.policy,
                "policy_conflict": (
                    "YES" if result.policy_conflict else "NO"
                ),
                "eligible_classes": (
                    "|".join(eligible)
                ),
                "candidate_classes": (
                    "|".join(candidates)
                ),
                "independently_cheapest_class": (
                    independent_cheapest_class
                ),
                "recommendation_check": (
                    "PASS" if recommendation_ok else "FAIL"
                ),
                "cost_check": "PASS" if cost_ok else "FAIL",
                "current_cost_check": (
                    "PASS" if current_cost_ok else "FAIL"
                ),
                "savings_check": (
                    "PASS" if savings_ok else "FAIL"
                ),
                "conflict_check": (
                    "PASS" if conflict_ok else "FAIL"
                ),
                "independent_cheapest_cost": (
                    f"{independent_cheapest_cost:.8f}"
                ),
                "independent_recommended_cost": (
                    f"{recommended_cost_independent:.8f}"
                    if recommended_cost_independent
                    != float("inf")
                    else "N/A"
                ),
                "independent_current_cost": (
                    f"{independent_current_cost:.8f}"
                ),
            }
        )

    # Write the complete per-record CSV report.
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)

    if rows:
        fieldnames = [
            "document_id",
            "document_state",
            "file_size_bytes",
            "current_storage_class",
            "access_count",
            "access_frequency",
            "days_since_last_access",
            "recommended_storage_class",
            "current_annual_cost",
            "recommended_annual_cost",
            "savings",
            "policy",
            "policy_conflict",
            "eligible_classes",
            "candidate_classes",
            "independently_cheapest_class",
            "recommendation_check",
            "cost_check",
            "current_cost_check",
            "savings_check",
            "conflict_check",
            "independent_cheapest_cost",
            "independent_recommended_cost",
            "independent_current_cost",
        ]

        with CSV_FILE.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.DictWriter(
                file, fieldnames=fieldnames
            )

            writer.writeheader()
            writer.writerows(rows)

    # Savings verdicts are recomputed from the per-record rows.
    savings_verdicts = Counter(
        classify(float(row["savings"]))
        for row in rows
        if row.get("savings")
    )

    print()
    print("=" * 62)
    print("INDEPENDENT AUDIT SUMMARY")
    print("=" * 62)
    print(f"Total records:            {len(items)}")
    print(f"PASS:                     {pass_count}")
    print(f"FAIL:                     {fail_count}")
    print(f"Recommendation mismatches: {recommendation_mismatches}")
    print(f"Cost mismatches:          {cost_mismatches}")
    print(f"Current-cost mismatches:  {current_cost_mismatches}")
    print(f"Savings mismatches:       {savings_mismatches}")
    print(f"Conflict-flag mismatches: {conflict_mismatches}")
    print("=" * 62)
    print("COUNTS BY RECOMMENDED TIER:")

    for storage_class in sorted(recommended_counts):
        print(
            f"  {storage_class:<28}"
            f"{recommended_counts[storage_class]:>5}"
        )

    print("COUNTS BY DOCUMENT STATE:")

    for state in sorted(state_counts):
        print(
            f"  {state:<28}"
            f"{state_counts[state]:>5}"
        )

    print("SAVINGS VERDICTS:")

    for verdict in ["POSITIVE SAVINGS", "NO CHANGE", "NEGATIVE SAVINGS"]:
        print(
            f"  {verdict:<28}"
            f"{savings_verdicts.get(verdict, 0):>5}"
        )

    print("=" * 62)
    print(f"Per-record CSV report: {CSV_FILE}")

    if fail_details:
        print()
        print("=" * 62)
        print(f"FAILING DOCUMENTS ({len(fail_details)}):")

        for detail in fail_details:
            print()
            print(f"--- {detail['document_id']} ---")

            if "error" in detail:
                print(f"  optimizer error: {detail['error']}")
                continue

            print(
                f"  optimizer recommended:   "
                f"{detail['optimizer_recommended']}"
            )
            print(
                f"  optimizer rec cost:      "
                f"${detail['optimizer_recommended_cost']:.8f}"
            )
            print(
                f"  optimizer current cost:  "
                f"${detail['optimizer_current_cost']:.8f}"
            )
            print(
                f"  optimizer savings:       "
                f"${detail['optimizer_savings']:.8f}"
            )
            print(
                f"  independent cheapest:    "
                f"{detail['independent_cheapest']}"
                f" (${detail['independent_cheapest_cost']:.8f})"
            )
            print(
                f"  independent rec cost:    "
                f"${detail['independent_recommended_cost']:.8f}"
            )
            print(
                f"  eligible:                "
                f"{', '.join(detail['eligible'])}"
            )
            print(
                f"  checks: "
                f"recommendation={detail['recommendation_check']} "
                f"cost={detail['cost_check']} "
                f"savings={detail['savings_check']}"
            )
            print("  independent costs per eligible tier:")

            for storage_class, costs in detail["row"].items():
                print(
                    f"    {storage_class:<28}"
                    f"${costs['total_cost']:.8f}"
                )

    print()
    print(
        "AUDIT RESULT: ALL RECORDS PASS"
        if fail_count == 0
        else "AUDIT RESULT: FAILURES FOUND"
    )

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())