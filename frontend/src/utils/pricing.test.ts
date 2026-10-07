// Tests for the client-side cost ESTIMATOR in pricing.ts. The
// expected values mirror optimization/costs.py::calculate_billable_size_gb
// over optimization/pricing.json — they pin the estimator to the
// engine's pricing snapshot, so a rate drift breaks a test instead of
// silently disagreeing with the engine.

import { describe, expect, it } from "vitest";
import type { ApiDocument } from "../types";
import bundle from "../data/experiments.json";
import {
  PRICING_CHECKED,
  PRICING_CURRENCY,
  PRICING_REGION,
  billableGb,
  estimateLiveStorageCost,
  monthlyCost,
} from "./pricing";

const GB = 1_000_000_000;

function doc(fileSize: number, storageClass: string): ApiDocument {
  return {
    document_id: `d-${storageClass}-${fileSize}`,
    object_key: "k",
    file_name: "f",
    file_size: fileSize,
    content_type: "text/plain",
    upload_timestamp: "2026-01-01T00:00:00Z",
    current_storage_class: storageClass,
    document_state: null,
  };
}

describe("billableGb mirrors calculate_billable_size_gb", () => {
  it("bills the file size when above the minimum", () => {
    expect(billableGb("STANDARD", 5 * GB)).toBe(5);
    expect(billableGb("STANDARD_IA", 2 * GB)).toBe(2);
  });

  it("applies the 128 KB minimum for IA-class tiers", () => {
    expect(billableGb("STANDARD_IA", 1_000)).toBe(128_000 / GB);
    expect(billableGb("GLACIER_INSTANT_RETRIEVAL", 1_000)).toBe(128_000 / GB);
    // Standard has no floor.
    expect(billableGb("STANDARD", 1_000)).toBe(1_000 / GB);
  });

  it("applies the 32 KB floor and 32 KB glacier overhead for archive tiers", () => {
    expect(billableGb("GLACIER_FLEXIBLE_RETRIEVAL", 1_000)).toBe(64_000 / GB);
    expect(billableGb("GLACIER_DEEP_ARCHIVE", 40_000)).toBe(72_000 / GB);
  });

  it("bills real size in Intelligent-Tiering (no per-object floor)", () => {
    // IT bills the actual object size; its 128 KB figure gates
    // tiering eligibility and the monitoring fee, not billing.
    expect(billableGb("INTELLIGENT_TIERING", 1_000)).toBe(1_000 / GB);
    expect(billableGb("INTELLIGENT_TIERING", 3 * GB)).toBe(3);
  });
});

describe("monthlyCost mirrors pricing.json rates", () => {
  it("uses the snapshot rate per class", () => {
    expect(monthlyCost("STANDARD", 10 * GB)).toBeCloseTo(10 * 0.025, 9);
    // Archive tiers add the 32 KB glacier-rate overhead to billable
    // size and the 8 KB Standard-rate portion billed for one month.
    expect(monthlyCost("GLACIER_DEEP_ARCHIVE", 10 * GB)).toBeCloseTo(
      ((10 * GB + 32_000) / GB) * 0.002 + (8_000 / GB) * 0.025,
      9,
    );
  });

  it("bills from the minimum for tiny objects", () => {
    expect(monthlyCost("STANDARD_IA", 500)).toBeCloseTo(
      (128_000 / GB) * 0.0138,
      9,
    );
  });

  it("estimates Intelligent-Tiering at the Frequent rate (blend upper bound) plus monitoring", () => {
    // Tiering-eligible object: frequent-rate storage + $0.0025/1,000
    // object-month monitoring fee.
    expect(monthlyCost("INTELLIGENT_TIERING", 10 * GB)).toBeCloseTo(
      10 * 0.025 + 0.0025 / 1000,
      9,
    );
    // Sub-threshold object: no monitoring fee, real size billed.
    expect(monthlyCost("INTELLIGENT_TIERING", 50_000)).toBeCloseTo(
      (50_000 / GB) * 0.025,
      9,
    );
  });
});

describe("estimateLiveStorageCost", () => {
  it("groups by tier and totals", () => {
    const estimate = estimateLiveStorageCost([
      doc(10 * GB, "STANDARD"),
      doc(4 * GB, "STANDARD"),
      doc(8 * GB, "GLACIER_DEEP_ARCHIVE"),
    ]);
    expect(estimate.tiers).toHaveLength(2);
    expect(estimate.tiers[0].tier).toBe("STANDARD");
    expect(estimate.tiers[0].documentCount).toBe(2);
    expect(estimate.tiers[0].billableGb).toBe(14);
    expect(estimate.tiers[0].monthlyCost).toBeCloseTo(14 * 0.025, 12);
    expect(estimate.tiers[1].tier).toBe("GLACIER_DEEP_ARCHIVE");
    expect(estimate.totalMonthly).toBeCloseTo(
      14 * 0.025 +
        ((8 * GB + 32_000) / GB) * 0.002 +
        (8_000 / GB) * 0.025,
      6, // tier values are round7-ed before summing
    );
    expect(estimate.totalAnnual).toBeCloseTo(estimate.totalMonthly * 12, 5);
    expect(estimate.totalBillableGb).toBeCloseTo(22 + 32_000 / GB, 9);
  });

  it("folds unknown classes into one Standard-rate bucket", () => {
    const estimate = estimateLiveStorageCost([doc(2 * GB, "REDUCED_REDUNDANCY")]);
    expect(estimate.tiers).toHaveLength(1);
    expect(estimate.tiers[0].tier).toBe("Unknown class");
    expect(estimate.tiers[0].monthlyCost).toBeCloseTo(2 * 0.025, 9);
    expect(estimate.unknownClassCount).toBe(1);
  });

  it("returns an empty estimate for an empty system", () => {
    const estimate = estimateLiveStorageCost([]);
    expect(estimate.tiers).toHaveLength(0);
    expect(estimate.totalMonthly).toBe(0);
    expect(estimate.totalAnnual).toBe(0);
    expect(estimate.totalBillableGb).toBe(0);
  });
});

describe("pricing provenance pins the bundle meta", () => {
  // The bundle meta is generated FROM optimization/pricing.json by
  // scripts/build_frontend_experiment_data.py, so this pins the
  // constants above to the engine snapshot: refresh the snapshot,
  // rebuild the bundle, and a stale hand-edit here fails.
  it("provenance constants match the pricing snapshot", () => {
    const meta = bundle.meta;
    expect(PRICING_REGION).toBe(meta.pricing_region);
    expect(PRICING_CURRENCY).toBe(meta.pricing_currency);
    expect(PRICING_CHECKED).toBe(meta.pricing_checked);
  });
});