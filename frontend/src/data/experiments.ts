// Typed accessor for the bundled experiment artifacts.
//
// Every value served from this module originates in a committed
// experiment artifact (see data/experiments.json meta.derived_from);
// the optimizer is a pure-Python engine run offline, so NO live
// optimization totals exist over the API. UI must label this data
// EXPERIMENT and live data LIVE, never mixing them.

import type {
  ExperimentBundle,
  OptimizeRunDoc,
  SyntheticExperimentDoc,
} from "../types";

type BundlePromise = Promise<ExperimentBundle>;

let bundle: Promise<ExperimentBundle> | null = null;

// The offline runner serializes four header aggregates as JSON strings
// (projected costs and savings percentage). Coerce them at the data
// boundary so every consumer sees the number the type declares.
const NUMERIC_HEADER_FIELDS = [
  "current_projected_annual_cost",
  "recommended_projected_annual_cost",
  "projected_savings",
  "savings_percentage",
] as const;

function normalizeRunHeader(
  header: ExperimentBundle["real"]["header"],
): ExperimentBundle["real"]["header"] {
  const fixed = { ...header };
  for (const field of NUMERIC_HEADER_FIELDS) {
    const value = fixed[field];
    if (typeof value === "string") fixed[field] = Number(value);
  }
  return fixed;
}

export function loadExperiments(): BundlePromise {
  bundle ??= import("./experiments.json").then((raw) => {
    // The JSON literal's inferred type types the serialized header
    // aggregates as strings (the offline runner writes them that way)
    // while the public bundle type declares numbers. normalizeRunHeader
    // performs that coercion below, so the cast routes through unknown:
    // this module IS the data boundary.
    const data = raw.default as unknown as ExperimentBundle;
    data.real.header = normalizeRunHeader(data.real.header);
    data.diversified.header = normalizeRunHeader(data.diversified.header);
    return data as ExperimentBundle;
  });
  return bundle;
}

// Live documents reference corpus documents by the api_document_id the
// loader minted — the same string lives in DocumentsTable.document_id.
// Building this join lets the (live) documents table show its
// experiment-modeled recommendation side by side, badged EXPERIMENT.
export async function buildExperimentJoin(): Promise<
  Record<string, OptimizeRunDoc | SyntheticExperimentDoc>
> {
  const data = await loadExperiments();
  const join: Record<
    string,
    OptimizeRunDoc | SyntheticExperimentDoc
  > = {};

  // Field-level merge keeps the two doc shapes usable behind one map.
  for (const doc of Object.values(data.real.documents)) {
    join[doc.document_id] = doc;
  }
  for (const doc of Object.values(data.diversified.documents)) {
    join[doc.document_id] = doc;
  }
  for (const doc of Object.values(data.synthetic.documents)) {
    join[doc.document_id] = doc;
  }

  return join;
}