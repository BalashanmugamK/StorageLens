// Recommendation normalization + experiment-bundle integrity.
// The bundle numbers here are committed experiment artifacts; the
// assertions pin the invariants the UI's honest-data contract relies
// on (totals match, savings reconcile, every doc has a recommendation).

import { beforeAll, describe, expect, it } from "vitest";
import { toJoinedRecommendation } from "./hooks/useExperimentJoin";
import { loadExperiments } from "./data/experiments";
import { TIER_ORDER } from "./types";
import type { ExperimentBundle, OptimizeRunDoc } from "./types";

let bundle: ExperimentBundle;

beforeAll(async () => {
  bundle = await loadExperiments();
});

describe("toJoinedRecommendation", () => {
  it("normalizes an optimize-run row", () => {
    const run = Object.values(bundle.real.documents)[0] as OptimizeRunDoc;
    const rec = toJoinedRecommendation(run);
    expect(rec.recommendedTier).toBe(run.recommended_storage_class);
    expect(rec.savings).toBe(run.savings);
    expect(rec.eligibleClasses).toEqual(run.eligible_classes);
    expect(rec.policyConflict).toBe(
      !run.eligible_classes.includes(run.current_storage_class),
    );
  });

  it("normalizes a synthetic row", () => {
    const syn = Object.values(bundle.synthetic.documents)[0];
    const rec = toJoinedRecommendation(syn);
    expect(rec.recommendedTier).toBe(syn.optimizer_storage_class);
    expect(rec.savings).toBe(syn.optimizer_savings);
    expect(rec.eligibleClasses).toEqual([]);
    expect(rec.tierCosts).toBeUndefined();
    expect(rec.policyConflict).toBe(false);
  });
});

describe("experiment bundle integrity", () => {
  it.each([
    ["real", 500],
    ["diversified", 500],
    ["synthetic", 500],
  ] as const)("%s corpus has %i documents with recommendations", (key, count) => {
    const docs =
      key === "synthetic"
        ? Object.values(bundle.synthetic.documents)
        : Object.values(bundle[key].documents);
    expect(docs).toHaveLength(count);
  });

  it("synthetic summary reconciles: savings = baseline − optimizer", () => {
    const s = bundle.synthetic.summary;
    expect(s.optimizer_savings_vs_baseline).toBeCloseTo(
      s.baseline_cost - s.optimizer_cost,
      4,
    );
    expect(s.optimizer_savings_percentage_vs_baseline).toBeCloseTo(
      (100 * s.optimizer_savings_vs_baseline) / s.baseline_cost,
      2,
    );
  });

  it("real corpus header counts reconcile with its document rows", () => {
    const header = bundle.real.header;
    const docs = Object.values(bundle.real.documents);
    expect(header.optimizer_success).toBe(docs.length);
    expect(header.positive_savings_count).toBe(
      docs.filter((d) => d.verdict.toLowerCase().startsWith("positive")).length,
    );
    expect(header.negative_savings_count).toBe(
      docs.filter((d) => d.verdict.toLowerCase().startsWith("negative")).length,
    );
    expect(header.no_change_count).toBe(
      docs.filter((d) => d.verdict.toLowerCase().startsWith("no change")).length,
    );
    // Any sign/verdict divergence is sub-cent rounding, never a real mismatch.
    for (const doc of docs) {
      const positiveBySign = doc.savings > 0;
      const positiveByVerdict = doc.verdict.toLowerCase().startsWith("positive");
      if (positiveBySign !== positiveByVerdict) {
        expect(Math.abs(doc.savings)).toBeLessThan(0.01);
      }
    }
  });

  it("diversified header costs reconcile with its document rows", () => {
    const header = bundle.diversified.header;
    const docs = Object.values(bundle.diversified.documents);
    const currentSum = docs.reduce((s, d) => s + d.current_annual_cost, 0);
    const recommendedSum = docs.reduce((s, d) => s + d.recommended_annual_cost, 0);
    expect(header.current_projected_annual_cost).toBeCloseTo(currentSum, 2);
    expect(header.recommended_projected_annual_cost).toBeCloseTo(recommendedSum, 2);
    expect(header.projected_savings).toBeCloseTo(currentSum - recommendedSum, 2);
  });

  it("diversified verdict counts reconcile with savings signs", () => {
    const header = bundle.diversified.header;
    const docs = Object.values(bundle.diversified.documents);
    expect(header.positive_savings_count).toBe(
      docs.filter((d) => d.verdict.toLowerCase().startsWith("positive")).length,
    );
    expect(header.negative_savings_count).toBe(
      docs.filter((d) => d.verdict.toLowerCase().startsWith("negative")).length,
    );
    expect(header.no_change_count).toBe(
      docs.filter((d) => d.verdict.toLowerCase().startsWith("no change")).length,
    );
    for (const doc of docs) {
      const positiveBySign = doc.savings > 0;
      const positiveByVerdict = doc.verdict.toLowerCase().startsWith("positive");
      if (positiveBySign !== positiveByVerdict) {
        expect(Math.abs(doc.savings)).toBeLessThan(0.01);
      }
    }
  });

  it("every recommendation names a known storage tier", () => {
    for (const key of ["real", "diversified"] as const) {
      for (const doc of Object.values(bundle[key].documents)) {
        expect(TIER_ORDER).toContain(doc.current_storage_class);
        expect(TIER_ORDER).toContain(doc.recommended_storage_class);
      }
    }
    for (const doc of Object.values(bundle.synthetic.documents)) {
      expect(TIER_ORDER).toContain(doc.current_storage_class);
      expect(TIER_ORDER).toContain(doc.optimizer_storage_class);
    }
  });
});