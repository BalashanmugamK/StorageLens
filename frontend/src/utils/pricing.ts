// Client-side storage-cost ESTIMATOR for live documents. This is a
// display helper only — NOT a second pricing authority.
//
// Every rate, floor and overhead byte comes from ../data/pricing.json,
// which scripts/build_frontend_experiment_data.py copies BYTE-IDENTICAL
// from optimization/pricing.json (the same file the engine loads and
// the Lambda bundles — the three copies are checksum-equal and the
// vitest suite pins it). Nothing here is hand-entered.
//
// Billable size mirrors optimization/costs.py::calculate_billable_size_gb
// (GB = 1e9 bytes; billable = max(file_size, min_billable_object_bytes)
// + archive_overhead_bytes_at_glacier_rate). The
// archive_overhead_bytes_at_standard_rate portion is billed by the
// engine separately at S3 Standard rates (costs.py
// calculate_archive_overhead_cost) and is mirrored the same way below.

import type { ApiDocument } from "../types";
import snapshot from "../data/pricing.json";

export const PRICING_REGION: string = snapshot.region;
export const PRICING_CURRENCY: string = snapshot.currency;
export const PRICING_CHECKED: string = snapshot.pricing_checked;
export const PRICING_SOURCE: string =
  `optimizer pricing snapshot — ${snapshot.pricing_source}-validated ` +
  `values, ${PRICING_REGION}`;

interface PricingTier {
  storage_per_gb_month?: number;
  // Intelligent-Tiering bills a Poisson blend across its
  // Frequent/Infrequent/Archive-Instant layers (costs.py). This
  // estimator has no access history to compute the blend from,
  // so it uses the Frequent rate: the upper bound of the blend,
  // i.e. a conservative (higher) cost estimate.
  storage_per_gb_month_frequent?: number;
  min_billable_object_bytes?: number;
  archive_overhead_bytes_at_glacier_rate?: number;
  archive_overhead_bytes_at_standard_rate?: number;
  intelligent_tiering_object?: boolean;
  monitoring_automation_fee_per_1000_object_month?: number;
}

const STORAGE_CLASSES = snapshot.storage_classes as Record<
  string,
  PricingTier
>;

// Standard rate — the fallback for classes missing from the snapshot.
const STANDARD_RATE: number =
  STORAGE_CLASSES.STANDARD.storage_per_gb_month ?? 0.025;

const BYTES_PER_GB = 1_000_000_000; // engine convention (costs.py)

// Intelligent-Tiering monitoring/automation fee: $X per 1,000
// objects per month (from the snapshot), charged ONLY to objects at
// or above the tiering threshold. Mirrors costs.py
// calculate_intelligent_tiering_costs.
function itMonitoringFeePerObjectMonth(): number {
  const fee = STORAGE_CLASSES.INTELLIGENT_TIERING
    ?.monitoring_automation_fee_per_1000_object_month;
  if (!fee) return 0;
  return fee / 1000;
}

// The tiering-eligibility threshold (its min_billable_object_bytes):
// objects below it are not charged the monitoring fee. Note the
// difference from BILLABLE sizing below — Intelligent-Tiering bills
// an object's real size; the threshold only gates the fee.
const IT_TIERING_MIN_BYTES: number =
  STORAGE_CLASSES.INTELLIGENT_TIERING?.min_billable_object_bytes ?? 0;

interface TierInfo {
  rate: number;
  minBytes: number;
  glacierOverhead: number;
  standardOverhead: number;
  known: boolean;
}

function tierInfo(storageClass: string): TierInfo {
  const tier = STORAGE_CLASSES[storageClass];

  if (!tier) {
    // Unknown classes charge at the Standard rate as a conservative
    // fallback and surface as an "Unknown class" bucket in the UI —
    // no document is silently dropped.
    return {
      rate: STANDARD_RATE,
      minBytes: 0,
      glacierOverhead: 0,
      standardOverhead: 0,
      known: false,
    };
  }

  return {
    rate: tier.storage_per_gb_month_frequent ?? tier.storage_per_gb_month ?? STANDARD_RATE,
    // Intelligent-Tiering bills real object size: its
    // min_billable_object_bytes gates tiering eligibility and the
    // monitoring fee, not billing — so billing minBytes is 0 here
    // (costs.py bills IT on real size too).
    minBytes:
      storageClass === "INTELLIGENT_TIERING"
        ? 0
        : tier.min_billable_object_bytes ?? 0,
    glacierOverhead: tier.archive_overhead_bytes_at_glacier_rate ?? 0,
    standardOverhead: tier.archive_overhead_bytes_at_standard_rate ?? 0,
    known: true,
  };
}

/** Billable storage size GB for one object in one tier (costs.py mirror). */
export function billableGb(storageClass: string, fileBytes: number): number {
  const info = tierInfo(storageClass);
  const billableBytes =
    Math.max(Number(fileBytes) || 0, info.minBytes) + info.glacierOverhead;
  return billableBytes / BYTES_PER_GB;
}

/** Estimated monthly storage cost in dollars for one object in one tier. */
export function monthlyCost(storageClass: string, fileBytes: number): number {
  const info = tierInfo(storageClass);
  if (!info.known || storageClass === "STANDARD") {
    return billableGb(storageClass, fileBytes) * info.rate;
  }
  if (storageClass === "INTELLIGENT_TIERING") {
    // Frequent-rate estimate (blend upper bound) plus the monitoring
    // fee for tiering-eligible object sizes.
    const monitoring =
      (Number(fileBytes) || 0) >= IT_TIERING_MIN_BYTES
        ? itMonitoringFeePerObjectMonth()
        : 0;
    return billableGb(storageClass, fileBytes) * info.rate + monitoring;
  }
  // The engine bills the standard-rate overhead portion over the whole
  // planning horizon; a static monthly estimate applies exactly one
  // month of it. The amount is negligible but the mirror is exact.
  const standardOverheadGb = info.standardOverhead / BYTES_PER_GB;
  return billableGb(storageClass, fileBytes) * info.rate + standardOverheadGb * STANDARD_RATE;
}

export interface LiveTierCost {
  tier: string;
  documentCount: number;
  billableGb: number;
  monthlyCost: number;
  annualCost: number;
}

/** Snapshot class order, zero-count tiers dropped, unknown classes last. */
export const ESTIMATE_TIER_ORDER: string[] = Object.keys(STORAGE_CLASSES);

export function estimateLiveStorageCost(documents: ApiDocument[]): {
  tiers: LiveTierCost[];
  totalMonthly: number;
  totalAnnual: number;
  totalBillableGb: number;
  unknownClassCount: number;
} {
  const buckets = new Map<
    string,
    { documentCount: number; billableGb: number; monthly: number }
  >();

  for (const doc of documents) {
    const info = tierInfo(doc.current_storage_class);
    // Unknown classes fold into one honest "Unknown class" bucket,
    // charged at the Standard rate as a conservative fallback — no
    // document is silently dropped from the totals.
    const bucketKey = info.known ? doc.current_storage_class : "Unknown class";
    const billable = billableGb(doc.current_storage_class, doc.file_size);
    const monthly = monthlyCost(doc.current_storage_class, doc.file_size);
    const bucket = buckets.get(bucketKey) ?? {
      documentCount: 0,
      billableGb: 0,
      monthly: 0,
    };
    bucket.documentCount += 1;
    bucket.billableGb += billable;
    bucket.monthly += monthly;
    buckets.set(bucketKey, bucket);
  }

  const ordered = [
    ...ESTIMATE_TIER_ORDER.filter((tier) => buckets.has(tier)),
    ...[...buckets.keys()].filter(
      (tier) => !ESTIMATE_TIER_ORDER.includes(tier),
    ),
  ];

  const round7 = (value: number) => round(value, 7);

  const tiers: LiveTierCost[] = ordered.map((tier) => {
    const bucket = buckets.get(tier)!;
    return {
      tier,
      documentCount: bucket.documentCount,
      billableGb: round7(bucket.billableGb),
      monthlyCost: round7(bucket.monthly),
      annualCost: round7(bucket.monthly * 12),
    };
  });

  const totalMonthly = round7(
    tiers.reduce((sum, t) => sum + t.monthlyCost, 0),
  );
  return {
    tiers,
    totalMonthly,
    totalAnnual: round7(totalMonthly * 12),
    totalBillableGb: round7(
      tiers.reduce((sum, t) => sum + t.billableGb, 0),
    ),
    unknownClassCount:
      buckets.get("Unknown class")?.documentCount ?? 0,
  };
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}