// Joins live documents to their experiment-modeled recommendation by
// api_document_id. The join always carries EXPERIMENT provenance in
// the UI — it supplements live rows, it never replaces them.

import { useEffect, useState } from "react";
import { buildExperimentJoin } from "../data/experiments";
import type { OptimizeRunDoc, SyntheticExperimentDoc } from "../types";

export type CombinedExperimentDoc = OptimizeRunDoc | SyntheticExperimentDoc;

export interface JoinedRecommendation {
  recommendedTier: string;
  savings: number;
  savingsPercentage: number;
  verdict: string;
  accessCount: number;
  eligibleClasses: string[];
  documentState: string | null;
  tierCosts?: Record<string, number>;
  /** True when the document's current class is outside its state-eligible set. */
  policyConflict: boolean;
}

export function toJoinedRecommendation(
  doc: CombinedExperimentDoc,
): JoinedRecommendation {
  const synthetic = "optimizer_storage_class" in doc;
  // Field-level access with the per-shape keys, normalized for the UI.
  const recommendedTier = synthetic
    ? (doc as SyntheticExperimentDoc).optimizer_storage_class
    : (doc as OptimizeRunDoc).recommended_storage_class;
  const savings = synthetic
    ? (doc as SyntheticExperimentDoc).optimizer_savings
    : (doc as OptimizeRunDoc).savings;
  const savingsPercentage = synthetic
    ? (doc as SyntheticExperimentDoc).optimizer_savings_percentage
    : (doc as OptimizeRunDoc).savings_percentage;
  const verdict = synthetic
    ? savings > 0
      ? "POSITIVE SAVINGS"
      : "NO CHANGE"
    : (doc as OptimizeRunDoc).verdict;
  const eligibleClasses = synthetic
    ? []
    : (doc as OptimizeRunDoc).eligible_classes;
  const policyConflict = !synthetic
    ? eligibleClasses.includes((doc as OptimizeRunDoc).current_storage_class) === false &&
      eligibleClasses.length > 0
    : false;

  return {
    recommendedTier,
    savings,
    savingsPercentage,
    verdict,
    accessCount: synthetic ? 0 : (doc as OptimizeRunDoc).access_count,
    eligibleClasses,
    documentState: (doc as OptimizeRunDoc).document_state ?? null,
    tierCosts: synthetic ? undefined : (doc as OptimizeRunDoc).tier_costs,
    policyConflict,
  };
}

export function useExperimentJoin(): Record<
  string,
  CombinedExperimentDoc
> | null {
  const [join, setJoin] = useState<Record<string, CombinedExperimentDoc> | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    buildExperimentJoin().then((map) => {
      if (!cancelled) setJoin(map);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return join;
}