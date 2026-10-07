// Policies — Policy A (state-constrained argmin, the actual
// experiment artifact) vs Policy B (cost-safe). Policy B is derived
// deterministically from the same artifact: for every document the
// current class joins the candidate set, so a B recommendation is the
// cheapest of {current} ∪ eligible. This is exactly what the engine
// does under Policy B, computed transparently in the browser from
// committed artifact numbers.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowRight, Scale } from "lucide-react";
import type {
  ApiFleetProjection,
  ExperimentBundle,
  OptimizeRunDoc,
} from "../types";
import { loadExperiments } from "../data/experiments";
import { api } from "../services/api";
import { BarList } from "../components/charts/BarList";
import {
  ExperimentBadge,
  PageHeader,
} from "../components/ui/atoms";
import { formatMoney, formatPercent, tierLabel } from "../utils/format";

interface PolicyProfile {
  name: string;
  motto: string;
  description: string;
  annualCost: number | null;
  totalSavings: number;
  savingsPercentage: number | null;
  migrations: number;
  negativeSavings: number;
  conflictsFlagged: number;
  /** Documents where staying put was cheaper than any eligible class. */
  heldInPlace: number;
}

/** Derive Policy B for one document from its Policy A artifact row. */
function derivePolicyB(doc: OptimizeRunDoc) {
  const currentCost = doc.current_annual_cost;
  const eligibleCosts = doc.eligible_classes
    .map((tier) => doc.tier_costs[tier])
    .filter((cost) => cost !== undefined);
  const bestEligible = eligibleCosts.length
    ? Math.min(...eligibleCosts)
    : Infinity;

  // Under Policy B the current class is always a candidate; ties
  // resolve to staying put (no migration).
  const move = bestEligible < currentCost;
  const bestTier = move
    ? doc.eligible_classes.reduce((best, tier) => {
        const cost = doc.tier_costs[tier];
        return cost !== undefined && cost < doc.tier_costs[best]
          ? tier
          : best;
      }, doc.eligible_classes[0])
    : doc.current_storage_class;
  const bestCost = move ? bestEligible : currentCost;

  return {
    bestTier,
    bestCost,
    savings: currentCost - bestCost,
    conflict: !doc.eligible_classes.includes(doc.current_storage_class),
  };
}

export default function Policies() {
  const [bundle, setBundle] = useState<ExperimentBundle | null>(null);
  // The fleet-cost panel has two honestly-labeled scopes: the
  // committed experiment artifact (default, always available) and the
  // LIVE backend engine run (GET /fleet/projection per policy).
  const [scope, setScope] = useState<"experiment" | "live">("experiment");
  const [liveByPolicy, setLiveByPolicy] = useState<
    Partial<Record<"A" | "B", ApiFleetProjection>>
  >({});
  const [loadingLive, setLoadingLive] = useState(false);
  const [liveFailed, setLiveFailed] = useState(false);
  const liveRequested = useRef(false);

  useEffect(() => {
    loadExperiments().then(setBundle);
  }, []);

  useEffect(() => {
    if (scope !== "live" || liveRequested.current) return;
    liveRequested.current = true;
    setLoadingLive(true);
    Promise.all([api.getFleetProjection("A"), api.getFleetProjection("B")])
      .then(([a, b]) => setLiveByPolicy({ A: a, B: b }))
      .catch(() => setLiveFailed(true))
      .finally(() => setLoadingLive(false));
  }, [scope]);

  const derivation = useMemo(() => {
    if (!bundle) return null;
    const docs = Object.values(bundle.diversified.documents);
    const header = bundle.diversified.header;

    let bCost = 0;
    let bMigrations = 0;
    let bHeld = 0;
    let conflicts = 0;
    let example: OptimizeRunDoc | null = null;
    let examplePremium = 0;

    for (const doc of docs) {
      const derived = derivePolicyB(doc);
      bCost += derived.bestCost;
      if (derived.bestTier !== doc.current_storage_class) bMigrations += 1;
      else bHeld += 1;
      if (derived.conflict) conflicts += 1;

      // The cost of Policy A's migration over Policy B's hold on the
      // same document — its "compliance premium".
      const premium = derived.bestCost - doc.recommended_annual_cost;
      if (premium > examplePremium + 1e-12) {
        examplePremium = premium;
        example = doc;
      }
    }

    const policyA: PolicyProfile = {
      name: "Policy A",
      motto: "Cheapest eligible",
      description:
        "Strictly state-constrained: among the state-eligible classes the engine picks the absolute cheapest, even when the cheapest eligible tier costs more than staying put. A migration inside the eligible set is always taken when it is the argmin.",
      annualCost: header.recommended_projected_annual_cost,
      totalSavings: header.projected_savings,
      savingsPercentage: header.savings_percentage,
      migrations:
        docs.filter((d) => d.recommended_storage_class !== d.current_storage_class).length,
      negativeSavings: docs.filter(
        (d) => d.recommended_storage_class !== d.current_storage_class && d.savings < 0,
      ).length,
      conflictsFlagged: docs.filter(
        (d) => !d.eligible_classes.includes(d.current_storage_class),
      ).length,
      heldInPlace: docs.filter(
        (d) => d.recommended_storage_class === d.current_storage_class,
      ).length,
    };

    const policyB: PolicyProfile = {
      name: "Policy B",
      motto: "Cost-safe advisory",
      description:
        "The current class always joins the candidate set, so a cost-increasing migration is never recommended. When the current class is not state-eligible and is also the cheapest option, the document is held in place and the conflict is reported separately.",
      annualCost: bCost,
      totalSavings: header.current_projected_annual_cost - bCost,
      savingsPercentage:
        100 * (1 - bCost / header.current_projected_annual_cost),
      migrations: bMigrations,
      negativeSavings: 0,
      conflictsFlagged: conflicts,
      heldInPlace: bHeld,
    };

    return {
      header,
      policyA,
      policyB,
      example,
      examplePremium: example ? examplePremium : 0,
      documentCount: docs.length,
    };
  }, [bundle]);

  if (!bundle || !derivation) {
    return (
      <div className="page-in" aria-hidden="true">
        <div className="skeleton" style={{ height: 300, borderRadius: 14 }} />
      </div>
    );
  }

  const { policyA, policyB, header, example, examplePremium, documentCount } =
    derivation;

  return (
    <div className="page-in">
      <PageHeader
        title="Eligibility Policies"
        subtitle="The same corpus, the same cost engine — two answers to 'what is this document allowed to move to?'"
      >
        <ExperimentBadge note="Derived from the diversified-corpus experiment artifact" />
      </PageHeader>

      <p
        style={{
          color: "var(--text-2)",
          fontSize: 13,
          maxWidth: 760,
          lineHeight: 1.65,
          marginBottom: 20,
        }}
      >
        Policy results are computed over the diversified GovInfo corpus
        ({documentCount.toLocaleString()} documents with real storage states).
        Policy A values come directly from the committed experiment artifact;
        Policy B is derived in this page from each artifact row using the
        engine's own cost-safe rule. The offline audit re-derived all{" "}
        {header.aggregates_in_table.toLocaleString()} aggregate records
        independently and agreed on every recommendation and conflict flag.
      </p>

      {/* Policy columns */}
      <div className="grid-2">
        {[policyA, policyB].map((policy, idx) => {
          const isB = idx === 1;
          return (
            <section
              key={policy.name}
              className="panel panel-pad"
              style={
                isB
                  ? { borderColor: "var(--accent-border)" }
                  : undefined
              }
            >
              <div className="row" style={{ justifyContent: "space-between" }}>
                <div className="row" style={{ gap: 9 }}>
                  <Scale size={16} style={{ color: isB ? "var(--accent-strong)" : "var(--info)" }} />
                  <strong style={{ fontSize: 15 }}>{policy.name}</strong>
                </div>
                <span className="badge badge-neutral">{policy.motto}</span>
              </div>
              <p
                style={{
                  color: "var(--text-2)",
                  fontSize: 12.5,
                  marginTop: 8,
                  lineHeight: 1.6,
                  minHeight: 60,
                }}
              >
                {policy.description}
              </p>

              <div
                style={{
                  border: "1px solid var(--border)",
                  borderRadius: "var(--radius)",
                  background: "var(--panel-2)",
                  padding: "14px 16px",
                  marginTop: 12,
                }}
              >
                <div className="stat-label">Annual cost after policy</div>
                <div className="tnum" style={{ fontSize: 30, fontWeight: 650, letterSpacing: "-0.02em" }}>
                  {policy.annualCost !== null ? formatMoney(policy.annualCost) : "—"}
                </div>
                <div className="stat-delta good">
                  saves {formatMoney(policy.totalSavings)} / yr
                  {policy.savingsPercentage !== null &&
                    ` · ${formatPercent(policy.savingsPercentage)}`}
                </div>
              </div>

              <div
                className="mt-2"
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: 10,
                }}
              >
                {[
                  ["Migrations", policy.migrations],
                  ["Negative savings", policy.negativeSavings],
                  ["Policy conflicts", policy.conflictsFlagged],
                  ["Held in place", policy.heldInPlace],
                ].map(([label, value]) => (
                  <div
                    key={label as string}
                    style={{
                      border: "1px solid var(--border)",
                      borderRadius: "var(--radius-sm)",
                      padding: "10px 12px",
                    }}
                  >
                    <div className="stat-label" style={{ fontSize: 11 }}>
                      {label}
                    </div>
                    <div
                      className="tnum"
                      style={{
                        fontSize: 17,
                        fontWeight: 600,
                        marginTop: 2,
                        color:
                          label === "Negative savings" && (value as number) > 0
                            ? "var(--danger)"
                            : "var(--text)",
                      }}
                    >
                      {(value as number).toLocaleString()}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          );
        })}
      </div>

      {/* Cost comparison — experiment artifact or LIVE engine run */}
      <section className="panel mt-3">
        <div className="panel-head">
          <div>
            <div className="panel-title">Projected fleet annual cost</div>
            <div className="panel-note">Lower is better</div>
          </div>
          <div className="seg" role="tablist" aria-label="Data scope">
            <button
              type="button"
              role="tab"
              aria-selected={scope === "experiment"}
              className={scope === "experiment" ? "on" : undefined}
              onClick={() => setScope("experiment")}
            >
              Experiment
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={scope === "live"}
              className={scope === "live" ? "on" : undefined}
              onClick={() => setScope("live")}
            >
              Live
            </button>
          </div>
        </div>
        {scope === "live" ? (
          loadingLive ? (
            <div className="chart-body">
              <div className="skeleton" style={{ height: 120, borderRadius: 8 }} />
            </div>
          ) : liveFailed ? (
            <div className="chart-body">
              <p className="panel-note" style={{ lineHeight: 1.6 }}>
                The live fleet projection is unavailable — offline or signed
                out. Switch back to the Experiment scope, or sign in to query
                the optimizer API over this deployment's live aggregates.
              </p>
            </div>
          ) : liveByPolicy.A && liveByPolicy.B ? (
            <>
              <BarList
                entries={[
                  {
                    label: "Current tiers",
                    series: [
                      {
                        value: liveByPolicy.A.baseline_projected_annual,
                        color: "var(--series-current)",
                        name: "Current",
                      },
                    ],
                    callout: (
                      <span className="muted">
                        ({liveByPolicy.A.aggregated_documents.toLocaleString()}{" "}
                        live aggregates
                        {liveByPolicy.A.stale_aggregates > 0
                          ? ` · ${liveByPolicy.A.stale_aggregates.toLocaleString()} stale`
                          : ""
                        })
                      </span>
                    ),
                  },
                  {
                    label: "Policy A",
                    series: [
                      {
                        value: liveByPolicy.A.optimized_projected_annual,
                        color: "var(--info)",
                        name: "Policy A",
                      },
                    ],
                    callout: (
                      <span style={{ color: "var(--info)" }}>
                        saves {formatMoney(liveByPolicy.A.projected_savings)} / yr
                        (engine run)
                      </span>
                    ),
                  },
                  {
                    label: "Policy B",
                    series: [
                      {
                        value: liveByPolicy.B.optimized_projected_annual,
                        color: "var(--series-optimized)",
                        name: "Policy B",
                      },
                    ],
                    callout: (
                      <span style={{ color: "var(--accent-strong)" }}>
                        saves {formatMoney(liveByPolicy.B.projected_savings)} / yr
                        — never increases a document's cost
                      </span>
                    ),
                  },
                ]}
                formatValue={formatMoney}
                ariaLabel="Projected annual cost of the live fleet, current tiers versus policy A and policy B"
                tableCaption="The engine's modeled 12-month cost over this deployment's live aggregates, per policy."
              />
              <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
                {liveByPolicy.B.basis}
              </p>
            </>
          ) : (
            <div className="chart-body">
              <div className="skeleton" style={{ height: 120, borderRadius: 8 }} />
            </div>
          )
        ) : (
          <>
            <BarList
              entries={[
                {
                  label: "Current tiers",
                  series: [
                    { value: header.current_projected_annual_cost, color: "var(--series-current)", name: "Current" },
                  ],
                  callout: <span className="muted">(as loaded)</span>,
                },
                {
                  label: "Policy A",
                  series: [
                    { value: policyA.annualCost ?? 0, color: "var(--info)", name: "Policy A" },
                  ],
                  callout: policyA.negativeSavings > 0 ? (
                    <span style={{ color: "var(--danger)" }}>
                      includes {policyA.negativeSavings} cost-increasing moves
                    </span>
                  ) : undefined,
                },
                {
                  label: "Policy B",
                  series: [
                    { value: policyB.annualCost ?? 0, color: "var(--series-optimized)", name: "Policy B" },
                  ],
                  callout: <span style={{ color: "var(--accent-strong)" }}>never increases a document's cost</span>,
                },
              ]}
              formatValue={formatMoney}
              ariaLabel="Projected annual cost under current allocation, policy A and policy B"
              tableCaption="Modeled 12-month fleet cost under each eligibility policy."
            />
            <p className="panel-note" style={{ padding: "0 22px 16px", lineHeight: 1.6 }}>
              Diversified-corpus artifact over {documentCount.toLocaleString()}{" "}
              documents. For the deployment's actual fleet, switch to the Live
              scope — that is the engine run behind{" "}
              <span className="mono">GET /fleet/projection</span>.
            </p>
          </>
        )}
      </section>

      {/* Conflict explainer with a real example */}
      {example && (
        <section className="panel mt-3">
          <div className="panel-head">
            <div>
              <div className="panel-title">When the two policies disagree</div>
              <div className="panel-note">
                The costliest compliance premium in this corpus
              </div>
            </div>
          </div>
          <div style={{ padding: "16px 22px 20px" }}>
            <div
              className="row"
              style={{
                border: "1px solid var(--warning-border)",
                background: "var(--warning-dim)",
                borderRadius: "var(--radius)",
                padding: "14px 16px",
                fontSize: 13,
              }}
            >
              <AlertTriangle size={16} style={{ color: "var(--warning)", flex: "none" }} />
              <div style={{ flex: 1, color: "var(--text-2)" }}>
                <strong style={{ color: "var(--text)" }}>Policy conflict </strong>
                <span className="mono muted" style={{ marginLeft: 6 }}>
                  {example.manifest_document_id}
                </span>
              </div>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
                gap: 12,
                marginTop: 14,
              }}
            >
              {[
                ["Current tier", tierLabel(example.current_storage_class)],
                ["Configured state", example.document_state ?? "unknown"],
                [
                  "Policy A would move to",
                  tierLabel(example.recommended_storage_class),
                ],
                [
                  "Policy B keeps",
                  tierLabel(example.current_storage_class),
                ],
              ].map(([label, value]) => (
                <div
                  key={label}
                  style={{
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius-sm)",
                    padding: "10px 12px",
                    background: "var(--panel-2)",
                  }}
                >
                  <div className="stat-label" style={{ fontSize: 11 }}>
                    {label}
                  </div>
                  <div style={{ color: "var(--text)", fontWeight: 550, marginTop: 2, fontSize: 12.5 }}>
                    {value}
                  </div>
                </div>
              ))}
            </div>

            <p style={{ color: "var(--text-2)", fontSize: 12.5, marginTop: 14, lineHeight: 1.65 }}>
              The document's current storage class ({tierLabel(example.current_storage_class)})
              is not eligible for its configured state ({example.document_state ?? "unknown"}).
              Policy A migrates it to the cheapest eligible tier anyway
              ({tierLabel(example.recommended_storage_class)}), which raises the modeled
              12-month cost for this document — a{" "}
              <strong style={{ color: "var(--warning)" }}>
                compliance premium of {formatMoney(examplePremium)} / year
              </strong>
              . Policy B keeps the document in place and records the conflict
              instead. Under Policy B the count of flagged disagreements is{" "}
              {policyB.conflictsFlagged.toLocaleString()} of {documentCount.toLocaleString()}{" "}
              documents.
            </p>

            <Link
              to="/experiments"
              className="row"
              style={{ marginTop: 14, color: "var(--info)", fontSize: 13, gap: 5 }}
            >
              Inspect the underlying experiment <ArrowRight size={13} />
            </Link>
          </div>
        </section>
      )}
    </div>
  );
}