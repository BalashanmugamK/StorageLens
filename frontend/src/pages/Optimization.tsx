// Optimization Engine — an interactive pipeline of the actual
// recommendation path: access pattern → workload model → pricing →
// eligibility → cost engine → recommendation. Copy stays strictly
// faithful to the optimization/ modules; the pricing stage is
// explicitly NOT claimed as live.

import { useEffect, useState } from "react";
import { ChevronRight, Info } from "lucide-react";
import type { ApiAggregationStats, ExperimentBundle } from "../types";
import { loadExperiments } from "../data/experiments";
import { api } from "../services/api";
import { BarList } from "../components/charts/BarList";
import { ExperimentBadge, LiveBadge, PageHeader } from "../components/ui/atoms";
import { formatSeconds } from "../utils/format";
import { PRICING_REGION } from "../utils/pricing";

interface Stage {
  key: string;
  name: string;
  module: string;
  title: string;
  description: string;
  facts: { label: string; value: string }[];
}

const STAGES: Stage[] = [
  {
    key: "access-pattern",
    name: "Access pattern",
    module: "optimization/access.py · backend/lambdas/aggregates",
    title: "What actually got read",
    description:
      "The aggregation Lambda queries every document's DOWNLOAD events inside a fixed 30-day window, then writes a snapshot: access_count, access_frequency = count / 30, days_since_last_access. Access-history queries retry transient DynamoDB errors with bounded exponential backoff and run in a bounded thread pool.",
    facts: [
      { label: "Observation window", value: "30 days, fixed" },
      { label: "Event type", value: "DOWNLOAD only" },
      { label: "Frequency", value: "access_count ÷ 30" },
    ],
  },
  {
    key: "workload-model",
    name: "Workload model",
    module: "optimization/workload.py",
    title: "The snapshot the optimizer consumes",
    description:
      "Each aggregate record becomes an OptimizerInput with ten fields: size, current storage class, upload age, last-access recency, 30-day frequency and document state. The access forecast is the observed 30-day frequency projected over 365 days; recency-related features are extracted and surfaced, while the projected access load is driven by the observed 30-day frequency.",
    facts: [
      { label: "Forecast horizon", value: "12 months" },
      { label: "Recency", value: "exponential attenuation (0.5^(days/30))" },
      { label: "State", value: "ACTIVE / CLOSED / ARCHIVED / unknown" },
    ],
  },
  {
    key: "pricing",
    name: "AWS pricing",
    module: "optimization/pricing.json · scripts/refresh_pricing.py",
    title: "A validated snapshot — not live queries",
    description:
      "Rates come from a committed snapshot diffed against the AWS Price List API; scripts/refresh_pricing.py re-checks freshness and refuses a write when an expected refreshable line goes missing. The engine warns when the snapshot's pricing_checked date is older than 90 days. Pricing is never fetched at optimization runtime.",
    facts: [
      { label: "Region", value: PRICING_REGION },
      { label: "Provenance", value: "AWS Price List API snapshot" },
      { label: "Max age", value: "90 days (warns beyond)" },
    ],
  },
  {
    key: "eligibility",
    name: "Eligibility",
    module: "optimization/constraints.py",
    title: "States gate the candidate tiers",
    description:
      "The document's state constrains its candidate classes: ACTIVE → Standard, Standard-IA, Instant Retrieval; CLOSED → Standard-IA, Instant Retrieval, Flexible Retrieval; ARCHIVED → Instant Retrieval, Flexible Retrieval, Deep Archive; unknown state → all classes. Policy A takes the state set as-is; Policy B adds the current class so a cost-increasing migration is never recommended.",
    facts: [
      { label: "Policy A", value: "strictly state-constrained argmin" },
      { label: "Policy B", value: "cost-safe: current class joins the set" },
      { label: "Conflict flag", value: "current class outside eligible set" },
    ],
  },
  {
    key: "cost-engine",
    name: "Cost engine",
    module: "optimization/costs.py",
    title: "Per-tier projected annual cost",
    description:
      "Every candidate tier is projected over 12 months: storage + forecast retrieval fees + GET request charges + lifecycle transition requests, per 1,000 objects. The model carries the parts that matter for small or young legal files: minimum billable object sizes, archived-object metadata overhead, and early-deletion fees prorated on the minimum storage duration.",
    facts: [
      { label: "Min billable size", value: "128 KB SIA/IR · 32 KB archival" },
      { label: "Archived overhead", value: "40 KB metadata per object" },
      { label: "Early deletion", value: "prorated remaining duration" },
    ],
  },
  {
    key: "recommendation",
    name: "Recommendation",
    module: "optimization/optimizer.py · optimization/metrics.py",
    title: "Cheapest eligible tier — with an escape hatch",
    description:
      "The engine picks the lowest-cost candidate and records savings vs the current tier. Restore waits (≈5 h Flexible, ≈12 h Deep Archive standard) are surfaced informationally as recommended_retrieval_time_hours and never gate the choice. When the current class is not state-eligible the result carries policy_conflict: true instead of silently forcing a move.",
    facts: [
      { label: "Selection", value: "argmin projected cost" },
      { label: "Restore waits", value: "informational only" },
      { label: "Conflicts", value: "flagged, never hidden" },
    ],
  },
];

const TIER_FACTS = [
  {
    tier: "Standard-IA / Glacier Instant Retrieval",
    detail: "Minimum billable object size of 128 KB — a 10 KB file still bills as 128 KB.",
  },
  {
    tier: "Glacier Flexible / Deep Archive",
    detail: "Minimum billable size of 32 KB, plus 40 KB of archived-object metadata (8 KB billed at Standard rates, 32 KB at the archive rate).",
  },
  {
    tier: "Early-deletion window",
    detail: "Moving out inside the minimum duration (30 d SIA, 90 d Instant Retrieval / Flexible, 180 d Deep Archive) bills the remaining days prorated on the current tier.",
  },
  {
    tier: "Destination exits",
    detail: "destination_early_exits_modeled is false — fees for leaving the destination tier are intentionally out of scope; the engine models the entrance, not the exit.",
  },
];

export default function OptimizationPage() {
  const [openKey, setOpenKey] = useState<string | null>("pricing");
  const [bundle, setBundle] = useState<ExperimentBundle | null>(null);
  // Live aggregation-pipeline state (GET /aggregates/stats) shown
  // beside — never instead of — the benchmark artifact below.
  const [liveStats, setLiveStats] = useState<ApiAggregationStats | null>(null);
  const [liveFailed, setLiveFailed] = useState(false);

  useEffect(() => {
    loadExperiments().then(setBundle);
    api
      .getAggregationStats()
      .then(setLiveStats)
      .catch(() => setLiveFailed(true));
  }, []);

  const benchmark = bundle?.aggregation_benchmark;
  const relevant = benchmark?.runs ?? [];
  const biggestBatch = Math.max(
    ...relevant.map((r) => r.documents),
    0,
  );
  const sequential = relevant.find(
    (r) => r.pattern === "sequential" && r.documents === biggestBatch,
  );
  const parallel = relevant.find(
    (r) => r.pattern === "parallel" && r.documents === biggestBatch,
  );

  const open = STAGES.find((s) => s.key === openKey);

  return (
    <div className="page-in">
      <PageHeader
        title="Optimization Engine"
        subtitle="Evaluate storage tiers against workload, pricing and policy constraints."
      />

      {/* Pipeline */}
      <section className="panel" style={{ padding: "20px 22px" }}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
          <div className="section-label">Recommendation pipeline</div>
          <span className="panel-note">Click a stage for what it actually does</span>
        </div>
        <div className="pipeline" role="list">
          {STAGES.map((stage, i) => (
            <div key={stage.key} className="row" style={{ flex: 1, minWidth: 0, gap: 0 }}>
              <button
                type="button"
                role="listitem"
                className={`pipe-node${openKey === stage.key ? " open" : ""}`}
                onClick={() => setOpenKey(openKey === stage.key ? null : stage.key)}
                aria-expanded={openKey === stage.key}
              >
                <div className="pipe-step">STAGE {i + 1}</div>
                <div className="pipe-name">{stage.name}</div>
              </button>
              {i < STAGES.length - 1 && (
                <span className="pipe-arrow" aria-hidden="true">
                  <ChevronRight size={16} />
                </span>
              )}
            </div>
          ))}
        </div>

        {open && (
          <div
            className="page-in"
            style={{
              marginTop: 16,
              border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
              background: "var(--panel-2)",
              padding: "16px 18px",
            }}
          >
            <div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>
                {open.name} — {open.title}
              </div>
              <div className="mono muted" style={{ marginTop: 3 }}>
                {open.module}
              </div>
            </div>
            <p
              style={{ color: "var(--text-2)", fontSize: 13, marginTop: 10, lineHeight: 1.65 }}
            >
              {open.description}
            </p>
            <div
              className="mt-2"
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
                gap: 10,
              }}
            >
              {open.facts.map((fact) => (
                <div
                  key={fact.label}
                  style={{
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius-sm)",
                    padding: "9px 12px",
                    background: "var(--panel)",
                  }}
                >
                  <div className="stat-label" style={{ fontSize: 11 }}>
                    {fact.label}
                  </div>
                  <div className="tnum" style={{ fontSize: 12.5, color: "var(--text)", marginTop: 1 }}>
                    {fact.value}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>

      <div className="grid-2 mt-3">
        {/* Pricing honesty */}
        <section className="panel panel-pad">
          <div className="section-label">Pricing honesty</div>
          <div className="row mt-2" style={{ alignItems: "flex-start", gap: 9 }}>
            <Info size={15} style={{ flex: "none", marginTop: 2, color: "var(--info)" }} />
            <p style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.65 }}>
              Projected annual costs come from the committed{" "}
              <span className="mono">optimization/pricing.json</span> snapshot,
              validated against the AWS Price List API for{" "}
              <strong style={{ color: "var(--text)" }}>{PRICING_REGION}</strong>. The
              frontend never claims runtime live pricing. Two rates (Deep
              Archive storage and restore requests) are documented as absent
              from the Price List API and keep their snapshot values.
            </p>
          </div>
          <div className="mt-3" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {TIER_FACTS.map((fact) => (
              <div
                key={fact.tier}
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 10,
                  fontSize: 12.5,
                  color: "var(--text-2)",
                }}
              >
                <span style={{ flex: "0 0 200px", color: "var(--text)", fontWeight: 550 }}>
                  {fact.tier}
                </span>
                <span style={{ lineHeight: 1.55, minWidth: 0 }}>{fact.detail}</span>
              </div>
            ))}
          </div>
        </section>

        {/* Benchmark + live pipeline state */}
        <section className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title">Aggregation performance</div>
              <div className="panel-note">
                {benchmark
                  ? `${benchmark.documents_available.toLocaleString()} documents · ${benchmark.region}`
                  : "Loading benchmark…"}
              </div>
            </div>
            <ExperimentBadge note="Measured from the aggregation benchmark artifact" />
          </div>
          {/* Live pipeline state from GET /aggregates/stats — shown
              beside the artifact, never merged into it. */}
          <div className="chart-body" style={{ paddingBottom: 12 }}>
            {liveStats ? (
              <div className="row" style={{ flexWrap: "wrap", gap: 10, alignItems: "center" }}>
                <LiveBadge />
                <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>
                  Live now:{" "}
                  <strong style={{ color: "var(--text)" }}>
                    {liveStats.aggregates.toLocaleString()}
                  </strong>{" "}
                  aggregates over{" "}
                  <strong style={{ color: "var(--text)" }}>
                    {liveStats.documents.toLocaleString()}
                  </strong>{" "}
                  documents ·{" "}
                  {liveStats.missing_aggregates === 0 ? (
                    "full coverage"
                  ) : (
                    <strong style={{ color: "var(--warning)" }}>
                      {liveStats.missing_aggregates.toLocaleString()} missing
                    </strong>
                  )}
                  {liveStats.last_aggregation_timestamp && (
                    <>
                      {" "}
                      · last run{" "}
                      {new Date(
                        liveStats.last_aggregation_timestamp,
                      ).toLocaleString()}
                    </>
                  )}
                </span>
              </div>
            ) : liveFailed ? (
              <div className="row" style={{ gap: 10, alignItems: "center" }}>
                <span style={{ fontSize: 12.5, color: "var(--warning)" }}>
                  Live aggregation stats unavailable — offline or signed out.
                  Only the benchmark artifact below is shown.
                </span>
              </div>
            ) : (
              <div className="skeleton" style={{ height: 22, borderRadius: 6, maxWidth: 460 }} />
            )}
          </div>
          {sequential && parallel ? (
            <>
              <BarList
                entries={[
                  {
                    label: "Sequential",
                    series: [
                      { value: sequential.seconds, color: "var(--series-current)", name: "Elapsed" },
                    ],
                  },
                  {
                    label: `Parallel (${benchmark?.parallel_workers ?? "?"} threads)`,
                    series: [
                      { value: parallel.seconds, color: "var(--series-optimized)", name: "Elapsed" },
                    ],
                  },
                ]}
                formatValue={formatSeconds}
                maxOverride={Math.max(sequential.seconds, parallel.seconds) * 1.05}
                ariaLabel="Aggregation wall-clock time, sequential versus parallel"
                tableCaption={`Wall-clock aggregation over ${sequential.documents.toLocaleString()} documents and ${sequential.aggregated_access_events.toLocaleString()} access events`}
              />
              <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
                {sequential.ms_per_document.toFixed(1)} ms per document sequential →{" "}
                {parallel.ms_per_document.toFixed(1)} ms per document parallel. The
                parallel path produces identical aggregate items, proven by the
                identity tests in tests/.
              </p>
            </>
          ) : (
            <div className="chart-body">
              <div className="skeleton" style={{ height: 96, borderRadius: 8 }} />
            </div>
          )}
        </section>
      </div>
    </div>
  );
}