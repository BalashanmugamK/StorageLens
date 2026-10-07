"""Build the experiment data files the StorageLens frontend bundles.

The optimization engine is a pure-Python engine run offline (there is
no live optimization API), so the frontend embeds trimmed, clearly
labeled copies of the committed experiment artifacts. This script reads
the source artifacts and writes the derived JSON files that
frontend/src/data experiments module imports.

Derived files live under frontend/src/data/ so their content ships with
the bundle; this regenerates them deterministically from the artifacts.

Usage:
    python scripts/build_frontend_experiment_data.py

Source artifacts (must exist):
    data/synthetic/workload_500_results.json        synthetic workload experiment
    data/real/diversified/manifest.json             diverse public legal corpus metadata
    data/real/diversified/workload_500.json         diversified workload snapshot
    experiments/results/diversified_500_results.json diversified experiment run
    experiments/results/real_500_results.json       real court-corpus experiment run
    experiments/results/aggregation_benchmark.json  aggregation performance benchmark

Writes:
    frontend/src/data/experiments.json
    frontend/src/data/pricing.json          verbatim pricing snapshot
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

with open(REPO_ROOT / "optimization" / "pricing.json", encoding="utf-8") as fh:
    PRICING = json.load(fh)

TIER_LABELS = {
    "STANDARD": "S3 Standard",
    "INTELLIGENT_TIERING": "Intelligent-Tiering",
    "STANDARD_IA": "Standard-IA",
    "GLACIER_INSTANT_RETRIEVAL": "Glacier Instant Retrieval",
    "GLACIER_FLEXIBLE_RETRIEVAL": "Glacier Flexible Retrieval",
    "GLACIER_DEEP_ARCHIVE": "Glacier Deep Archive",
}

TIER_ORDER = [
    "STANDARD",
    "INTELLIGENT_TIERING",
    "STANDARD_IA",
    "GLACIER_INSTANT_RETRIEVAL",
    "GLACIER_FLEXIBLE_RETRIEVAL",
    "GLACIER_DEEP_ARCHIVE",
]


def repo_path(rel):
    return REPO_ROOT / rel


def load_json(rel):
    with open(repo_path(rel), encoding="utf-8") as fh:
        return json.load(fh)


def parse_tier_costs(raw):
    """'CLASS=0.001 | CLASS2=0.002' -> {'CLASS': 0.001, ...}"""
    parsed = {}
    for pair in raw.split("|"):
        pair = pair.strip()
        if not pair:
            continue
        tier, cost = pair.split("=", 1)
        parsed[tier.strip()] = float(cost)
    return parsed


def round7(value):
    return round(value, 7)


def trim_run(source):
    """Trim one run_real_optimizer.py artifact into a frontend record."""
    docs = {}
    for row in source["results"]:
        tier_costs = parse_tier_costs(row["tier_costs"])
        docs[row["manifest_document_id"]] = {
            "manifest_document_id": row["manifest_document_id"],
            "document_id": row["document_id"],
            "file_size_bytes": row["file_size_bytes"],
            "document_state": row.get("document_state") or None,
            "court": row.get("court") or None,
            "date_issued": row.get("date_issued") or None,
            "current_storage_class": row["current_storage_class"],
            "access_count": row["access_count"],
            "access_frequency": round(
                float(row["access_frequency"] or 0), 6
            ),
            "days_since_last_access": (
                round(float(row["days_since_last_access"]), 3)
                if row.get("days_since_last_access") not in (None, "")
                else None
            ),
            "eligible_classes": row["eligible_classes"].split("|"),
            "recommended_storage_class": row["recommended_storage_class"],
            "current_annual_cost": round7(
                float(row["current_annual_cost"])
            ),
            "recommended_annual_cost": round7(
                float(row["recommended_annual_cost"])
            ),
            "savings": round7(float(row["savings"])),
            "savings_percentage": round7(
                float(row["savings_percentage"])
            ),
            "verdict": row["verdict"],
            "tier_costs": tier_costs,
        }
    return docs


def main():
    synthetic = load_json("data/synthetic/workload_500_results.json")
    diversified_run = load_json(
        "experiments/results/diversified_500_results.json"
    )
    real_run = load_json("experiments/results/real_500_results.json")
    diversified_manifest = load_json("data/real/diversified/manifest.json")
    diversified_workload = load_json(
        "data/real/diversified/workload_500.json"
    )
    benchmark = load_json(
        "experiments/results/aggregation_benchmark.json"
    )

    synthetic_summary = synthetic["summary"]
    synthetic_docs = {
        row["document_id"]: {
            "document_id": row["document_id"],
            "file_size_bytes": row["file_size_bytes"],
            "current_storage_class": row["current_storage_class"],
            "current_cost": round7(row["current_cost"]),
            "optimizer_storage_class": row["optimizer_storage_class"],
            "optimizer_cost": round7(row["optimizer_cost"]),
            "optimizer_savings": round7(
                row["optimizer_savings_vs_current"]
            ),
            "optimizer_savings_percentage": round7(
                row["optimizer_savings_percentage_vs_current"]
            ),
            "baseline_storage_class": row["baseline_storage_class"],
            "baseline_cost": round7(row["baseline_cost"]),
        }
        for row in synthetic["documents"]
    }

    diversified_docs = trim_run(diversified_run)
    real_docs = trim_run(real_run)

    # Enrich diversified rows with corpus metadata (collection, title)
    # from the manifest so the results browser can group by source.
    manifest_meta = {
        doc["document_id"]: doc
        for doc in diversified_manifest["documents"]
    }
    workload_meta = {
        doc["document_id"]: doc
        for doc in diversified_workload["documents"]
    }
    for key, row in diversified_docs.items():
        meta = manifest_meta.get(key)
        if meta:
            row["collection"] = meta.get("collection")
            row["collection_label"] = meta.get("collection_label")
            row["title"] = meta.get("title")
        wl = workload_meta.get(key)
        if wl:
            row["access_band"] = wl.get("access_band")
            row["recency_band"] = wl.get("recency_band")
            row["age_band"] = wl.get("age_band")

    # Savings-opportunity roll-ups per destination tier (Policy A run).
    tier_opportunities = []
    for tier in TIER_ORDER:
        rows = [
            r
            for r in diversified_docs.values()
            if r["recommended_storage_class"] == tier
            and r["savings"] > 0
        ]
        if not rows:
            continue
        tier_opportunities.append(
            {
                "tier": tier,
                "tier_label": TIER_LABELS[tier],
                "document_count": len(rows),
                "annual_savings": round7(
                    sum(r["savings"] for r in rows)
                ),
            }
        )
    tier_opportunities.sort(key=lambda t: -t["annual_savings"])

    # Real-corpus opportunities (all 94 -> Glacier Instant Retrieval).
    real_tier_opportunities = {}
    for row in real_docs.values():
        if row["savings"] > 0:
            tier = row["recommended_storage_class"]
            bucket = real_tier_opportunities.setdefault(
                tier,
                {
                    "tier": tier,
                    "tier_label": TIER_LABELS[tier],
                    "document_count": 0,
                    "annual_savings": 0.0,
                },
            )
            bucket["document_count"] += 1
            bucket["annual_savings"] = round7(
                bucket["annual_savings"] + row["savings"]
            )
            real_tier_opportunities[tier] = bucket

    payload = {
        "meta": {
            "generated_by": "scripts/build_frontend_experiment_data.py",
            "derived_from": [
                "data/synthetic/workload_500_results.json",
                "data/real/diversified/manifest.json",
                "data/real/diversified/workload_500.json",
                "experiments/results/diversified_500_results.json",
                "experiments/results/real_500_results.json",
                "experiments/results/aggregation_benchmark.json",
                "optimization/pricing.json",
            ],
            # Pricing provenance flows from the optimizer's own
            # snapshot, so the frontend never hardcodes a region or
            # currency (vitest pins this against src/utils/pricing.ts).
            "pricing_region": PRICING["region"],
            "pricing_currency": PRICING["currency"],
            "pricing_checked": PRICING["pricing_checked"],
            "pricing_source": PRICING["pricing_source"],
            "tier_labels": TIER_LABELS,
            "tier_order": TIER_ORDER,
            "model_versions": {
                # The synthetic run was regenerated with the
                # Intelligent-Tiering candidate; the diversified/
                # real runs predate it and are re-run from the live
                # aggregates table when AWS credentials are
                # available. Do NOT mix their numbers in one chart
                # without checking this field.
                "synthetic": "engine with INTELLIGENT_TIERING candidate",
                "diversified": "engine before INTELLIGENT_TIERING candidate",
                "real": "engine before INTELLIGENT_TIERING candidate",
            },
        },
        "synthetic": {
            "experiment": synthetic["experiment"],
            "summary": {
                key: value
                for key, value in synthetic_summary.items()
                if key
                in (
                    "total_documents",
                    "total_storage_gb",
                    "baseline_cost",
                    "optimizer_cost",
                    "optimizer_savings_vs_baseline",
                    "optimizer_savings_percentage_vs_baseline",
                    "baseline_tier_distribution",
                    "optimizer_tier_distribution",
                    "baseline_cost_breakdown",
                    "optimizer_cost_breakdown",
                    "baseline_transition_count",
                )
            },
            "documents": synthetic_docs,
        },
        "diversified": {
            "header": {
                key: value
                for key, value in diversified_run.items()
                if key != "results"
            },
            "documents": diversified_docs,
            "tier_opportunities": tier_opportunities,
        },
        "real": {
            "header": {
                key: value
                for key, value in real_run.items()
                if key != "results"
            },
            "corpus": {
                "document_count": len(real_docs),
                "total_bytes": sum(
                    r["file_size_bytes"] for r in real_docs.values()
                ),
                "source": real_run["source"],
                "year": sorted(
                    set(
                        r["date_issued"][:4]
                        for r in real_docs.values()
                        if r["date_issued"]
                    )
                ),
            },
            "documents": real_docs,
            "tier_opportunities": sorted(
                real_tier_opportunities.values(),
                key=lambda t: -t["annual_savings"],
            ),
        },
        "aggregation_benchmark": {
            "region": benchmark["region"],
            "documents_available": benchmark["documents_available"],
            # Worker count recorded by the benchmark itself, so the UI
            # never hardcodes a thread count.
            "parallel_workers": benchmark["parallel_workers"],
            "runs": benchmark["runs"],
        },
    }

    out_path = REPO_ROOT / "frontend" / "src" / "data" / "experiments.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.0f} KB)")

    # pricing.json is copied BYTE-IDENTICAL from optimization/pricing.json
    # so frontend/src/utils/pricing.ts reads the engine's authoritative
    # snapshot instead of hand-mirrored rate tables. A byte-identical
    # copy (not a re-serialization) keeps the three copies comparable
    # by checksum.
    import shutil

    pricing_path = REPO_ROOT / "frontend" / "src" / "data" / "pricing.json"
    shutil.copyfile(
        REPO_ROOT / "optimization" / "pricing.json",
        pricing_path,
    )
    print(f"wrote {pricing_path}")

    return None


if __name__ == "__main__":
    sys.exit(main())