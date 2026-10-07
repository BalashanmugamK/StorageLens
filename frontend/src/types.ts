// Type definitions for the live API and the bundled experiment data.

/* ---------- Live API ---------- */

// DocumentsTable item (backend/lambdas/documents/app.py)
export interface ApiDocument {
  document_id: string;
  object_key: string;
  file_name: string;
  file_size: number;
  content_type: string;
  upload_timestamp: string;
  current_storage_class: string;
  document_state?: string | null;
}

export interface ApiAccessStats {
  document_id: string;
  access_count: number;
  last_accessed: string | null;
  recent_access_count: number;
}

export interface ApiCreateDocument {
  document_id: string;
  upload_url: string;
  object_key: string;
}

export interface ApiDownload {
  document_id: string;
  download_url: string;
  access_timestamp: string;
  access_type: string;
}

export interface ApiAggregationResult {
  message: string;
  documents_processed: number;
}

// GET /aggregates/stats — live state of the aggregation pipeline
// (aggregation lambda, aggregation_summary).
export interface ApiAggregationStats {
  documents: number;
  aggregates: number;
  missing_aggregates: number;
  last_aggregation_timestamp: string | null;
  storage_class_distribution: Record<string, number>;
  access_window_days: number;
  basis: string;
}

// GET /fleet/projection — the engine run over every live aggregate
// (decisions lambda, fleet_projection).
export interface ApiFleetAllocation {
  storage_class: string;
  document_count: number;
  billable_gb: number | null;
}

export interface ApiFleetProjection {
  policy: string;
  documents: number;
  aggregated_documents: number;
  stale_aggregates: number;
  baseline_projected_annual: number;
  optimized_projected_annual: number;
  projected_savings: number;
  savings_percentage: number;
  migrations: number;
  migrations_by_target: Record<
    string,
    { count: number; savings_annual: number }
  >;
  current_tier_allocation?: ApiFleetAllocation[];
  recommended_tier_allocation?: ApiFleetAllocation[];
  basis: string;
}

/* ---------- Decisions: approval → execution → ledger ---------- */

export type DecisionState =
  | "proposed"
  | "approved"
  | "rejected"
  | "verified"
  | "failed";

// The feature summary the engine consumed, recorded for reviewer
// provenance (decisions lambda, create_decision).
export interface ApiEngineInputSummary {
  access_count: number;
  access_frequency: number;
  days_since_last_access: number;
}

export interface ApiDecision {
  decision_id: string;
  document_id: string;
  object_key: string;
  file_name: string | null;
  file_size: number;
  from_class: string;
  to_class: string;
  predicted_from_cost: number;
  predicted_to_cost: number;
  predicted_savings: number;
  savings_percentage: number;
  policy: string;
  policy_conflict: boolean;
  features_as_of: string | null;
  engine_input?: ApiEngineInputSummary;
  decision_state: DecisionState;
  created_at: string;
  approved_at?: string;
  rejected_at?: string;
  executed_at?: string;
  verified_at?: string;
  verified_storage_class?: string;
  verified_size?: number;
  realized_savings?: number;
  verification_error?: string;
}

export interface ApiDecisionList {
  decisions: ApiDecision[];
}

// POST /decisions/propose-batch — bulk proposal recording.
export interface ApiProposeBatchResult {
  proposed_count: number;
  proposed: ApiDecision[];
  skipped: { document_id: string; reason: string }[];
  aggregates_scanned: number;
  basis: string;
}

export interface ApiTransitionResult {
  decision_id: string;
  decision_state: DecisionState;
}

// The only transition that touches S3: on success the object's HEAD
// verified the approved class (realized = predicted carried by the
// decision); on failure the verification_error explains why.
export interface ApiExecuteResult {
  decision_id: string;
  decision_state: "verified" | "failed";
  verified_storage_class?: string;
  verified_size?: number;
  realized_savings?: number;
  verification_error?: string;
}

export interface ApiLedger {
  entries: ApiDecision[];
  total_realized_annual_savings: number;
  // Honest-labeling string from the backend: the totals are predicted
  // annual savings carried by HEAD-verified transitions, never
  // invoice data.
  basis: string;
}

/* ---------- Ingest: the bridge to an existing customer bucket ---------- */

// POST /imports/inventory — one S3 Inventory manifest becomes
// DocumentsTable rows (deterministic uuid5 ids, idempotent re-import).
export interface ApiImportInventory {
  documents_imported: number;
  documents_skipped_unsupported_class: number;
  documents_skipped_invalid: number;
  unsupported_class_examples: string[];
  truncated: boolean;
  row_cap: number;
  basis: string;
}

// POST /imports/access-logs — successful GETs in S3 server access
// logs become AccessHistoryTable DOWNLOAD events with log timestamps.
export interface ApiImportAccessLogs {
  events_recorded: number;
  files_scanned: number;
  files_failed: string[];
  truncated: boolean;
  file_cap: number;
  basis: string;
}

// POST /imports/check — the onboarding preflight. Bounded reads only,
// nothing imported: is the bucket reachable, is there an inventory
// manifest (verified key or discovered schema.csv candidates), and do
// objects under the log prefix parse like S3 access logs.
export interface ApiImportCheck {
  bucket: string;
  reachable: boolean | null;
  inventory: {
    mode?: "checked" | "discovered";
    key?: string;
    exists?: boolean;
    size_bytes?: number | null;
    columns_ok?: boolean | null;
    candidates?: string[];
    missing_columns?: string[];
    discovery_limited?: boolean;
    notes: string[];
  };
  access_logs: {
    prefix: string;
    files_found?: number;
    listing_truncated?: boolean;
    // Up to a few objects probed for the format; stops at the first
    // with parseable lines (listing order is lexicographic, so the
    // first key may be the inventory schema, not a log).
    sampled_files?: string[];
    sample_parse_success?: number | null;
    notes: string[];
  };
  ready: boolean;
  notes?: string[];
  basis: string;
}

/* ---------- Reconciliation: the model vs the actual bill ---------- */

// One month of the reconciliation report (POST /reconciliation/run,
// reconciliation lambda). Bucket keys are model classes plus the two
// non-model buckets, "requests_and_transfer" and "unmapped_storage".
export interface ApiReconciliationMonth {
  month: string;
  actual_usd: Record<string, number>;
  predicted_usd: Record<string, number>;
  variance_usd: Record<string, number>;
  actual_non_model_usd: number;
  estimated_month: boolean;
}

export interface ApiReconciliationReport {
  ran_at: string;
  months_requested: number;
  months: ApiReconciliationMonth[];
  totals: {
    predicted_storage_usd: number;
    actual_storage_usd: number;
    // predicted - actual (positive: model expects more spend than
    // the bill shows)
    variance_usd: number;
    actual_non_storage_usd: number;
    months_estimated: string[];
  };
  // The approval side's claim, recorded as context — the report
  // keeps ledger and bill numbers side by side, attribution of a
  // bill delta to individual transitions is NOT attempted.
  ledger: {
    verified_decisions: number;
    realized_savings_annual: number;
  };
  prediction_errors: { document_id: string | null; error: string }[];
  prediction_failures: number;
  basis: string;
}

/* ---------- On-Call: shift schedule + PagerDuty sync + paging audit ---------- */

// GET /oncall/me — who the authorizer thinks the caller is. can_manage
// gates every write control (shifts, sync); principal is the email or
// Cognito sub the JWT resolved to.
export interface ApiOnCallMe {
  principal: string;
  can_manage: boolean;
}

export type OnCallRole = "primary" | "secondary";

// Why a shift did or did not reach the PagerDuty schedule. skipped_*
// is diagnostic (nothing was attempted), removed marks a delete that
// was confirmed against the schedule, failed needs attention.
export type OnCallSyncStatus =
  | "pending"
  | "synced"
  | "skipped_missing_pd_user_id"
  | "skipped_no_secret"
  | "skipped_no_schedule"
  | "failed"
  | "removed";

// One OnCall schedule row (backend/lambdas/oncall/app.py). start_at /
// end_at are ISO-8601 UTC and windows are half-open: start_at <= now
// < end_at covers now.
export interface ApiOnCallShift {
  shift_id: string;
  engineer_email: string;
  engineer_name?: string;
  pagerduty_user_id?: string;
  role: OnCallRole;
  start_at: string;
  end_at: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  sync_status: OnCallSyncStatus;
  sync_error?: string;
  last_synced_at?: string;
}

// POST/PUT /oncall/shifts — the writable fields; shift_id and the
// stamps come from the path or the authorizer.
export interface ApiOnCallShiftInput {
  engineer_email: string;
  engineer_name?: string;
  pagerduty_user_id?: string;
  role: OnCallRole;
  start_at: string;
  end_at: string;
}

export interface ApiOnCallShiftList {
  shifts: ApiOnCallShift[];
  next_token?: string;
}

export interface ApiOnCallDeleteResult {
  deleted: string;
}

// GET /oncall/current — the live coverage resolution: per role the
// newest covering start_at wins; any additional covering shift for the
// role is carried in overlapping (a UI warning, not an error).
export interface ApiOnCallCoverage {
  now: string;
  primary: ApiOnCallShift | null;
  secondary: ApiOnCallShift | null;
  overlapping: ApiOnCallShift[];
  coverage: "covered" | "none";
}

// POST /oncall/sync — the PagerDuty schedule reconcile. skipped_no_*
// means nothing was attempted (credential/schedule missing); counts
// are present only on an attempted reconcile.
export interface ApiOnCallSyncResult {
  pd_status:
    | "success"
    | "failed"
    | "skipped_no_secret"
    | "skipped_no_schedule";
  added?: number;
  removed?: number;
  skipped?: number;
  failed?: number;
  detail?: string;
}

// The engineer stamp a paging audit row carries: who was on call when
// the alarm fired (null where that role was uncovered — unrouted).
export interface ApiOnCallEngineer {
  email: string;
  name?: string;
  pagerduty_user_id?: string;
}

// One paging audit row: a CloudWatch alarm notification and whether a
// PagerDuty incident came out of it (routing routed/unrouted).
export interface ApiOnCallNotification {
  notification_id: string;
  created_at: string;
  alarm_name: string;
  alarm_arn?: string;
  state_value?: string;
  event_action?: string;
  pd_status?: string;
  pd_incident_dedup_key?: string;
  routing?: "routed" | "unrouted";
  severity?: string;
  summary?: string;
  oncall_primary?: ApiOnCallEngineer | null;
  oncall_secondary?: ApiOnCallEngineer | null;
}

export interface ApiOnCallNotificationList {
  notifications: ApiOnCallNotification[];
  next_token?: string;
}

/* ---------- Bundled experiment data ---------- */

export const TIER_LABELS: Record<string, string> = {
  STANDARD: "S3 Standard",
  INTELLIGENT_TIERING: "Intelligent-Tiering",
  STANDARD_IA: "Standard-IA",
  GLACIER_INSTANT_RETRIEVAL: "Glacier Instant Retrieval",
  GLACIER_FLEXIBLE_RETRIEVAL: "Glacier Flexible Retrieval",
  GLACIER_DEEP_ARCHIVE: "Glacier Deep Archive",
};

export const TIER_ORDER = [
  "STANDARD",
  "INTELLIGENT_TIERING",
  "STANDARD_IA",
  "GLACIER_INSTANT_RETRIEVAL",
  "GLACIER_FLEXIBLE_RETRIEVAL",
  "GLACIER_DEEP_ARCHIVE",
] as const;

export interface CostBreakdown {
  storage_cost: number;
  retrieval_cost: number;
  request_cost: number;
  transition_cost: number;
  total_cost: number;
}

export interface TierDistributionEntry {
  document_count: number;
  storage_gb: number;
}

export interface SyntheticExperimentDoc {
  document_id: string;
  file_size_bytes: number;
  current_storage_class: string;
  current_cost: number;
  optimizer_storage_class: string;
  optimizer_cost: number;
  optimizer_savings: number;
  optimizer_savings_percentage: number;
  baseline_storage_class: string;
  baseline_cost: number;
}

export interface SyntheticExperiment {
  experiment: {
    workload_file: string;
    reference_timestamp: string;
    planning_horizon_months: number;
  };
  summary: {
    total_documents: number;
    total_storage_gb: number;
    baseline_cost: number;
    optimizer_cost: number;
    optimizer_savings_vs_baseline: number;
    optimizer_savings_percentage_vs_baseline: number;
    baseline_tier_distribution: Record<string, TierDistributionEntry>;
    optimizer_tier_distribution: Record<string, TierDistributionEntry>;
    baseline_cost_breakdown: CostBreakdown;
    optimizer_cost_breakdown: CostBreakdown;
    baseline_transition_count: number;
  };
  documents: Record<string, SyntheticExperimentDoc>;
}

export interface OptimizeRunDoc {
  manifest_document_id: string;
  document_id: string;
  file_size_bytes: number;
  document_state: string | null;
  court: string | null;
  date_issued: string | null;
  current_storage_class: string;
  access_count: number;
  access_frequency: number;
  days_since_last_access: number | null;
  eligible_classes: string[];
  recommended_storage_class: string;
  current_annual_cost: number;
  recommended_annual_cost: number;
  savings: number;
  savings_percentage: number;
  verdict: string;
  tier_costs: Record<string, number>;
  // Diversified-only enrichments
  collection?: string;
  collection_label?: string;
  title?: string;
  access_band?: string;
  recency_band?: string;
  age_band?: string;
}

export interface OptimizeRunHeader {
  dataset: string;
  source: string;
  region: string;
  aggregates_table: string;
  aggregates_in_table: number;
  documents_processed: number;
  access_events_on_record: number;
  optimizer_success: number;
  optimizer_errors: number;
  document_state_counts: Record<string, number>;
  current_storage_class_counts: Record<string, number>;
  recommended_storage_class_counts: Record<string, number>;
  current_projected_annual_cost: number;
  recommended_projected_annual_cost: number;
  projected_savings: number;
  savings_percentage: number;
  no_change_count: number;
  positive_savings_count: number;
  negative_savings_count: number;
}

export interface TierOpportunity {
  tier: string;
  tier_label: string;
  document_count: number;
  annual_savings: number;
}

export interface OptimizeExperiment {
  header: OptimizeRunHeader;
  documents: Record<string, OptimizeRunDoc>;
  tier_opportunities: TierOpportunity[];
}

export interface ExperimentBundle {
  meta: {
    generated_by: string;
    derived_from: string[];
    tier_labels: Record<string, string>;
    tier_order: string[];
    pricing_region: string;
    pricing_currency: string;
    pricing_checked: string;
    pricing_source: string;
    // Which runs already share the current engine's candidate set —
    // numbers from runs with different model_versions must never be
    // mixed in one chart without checking this field.
    model_versions: Record<string, string>;
  };
  synthetic: SyntheticExperiment;
  diversified: OptimizeExperiment & {
    header: OptimizeRunHeader & { methodology?: string };
  };
  real: OptimizeExperiment & {
    corpus: {
      document_count: number;
      total_bytes: number;
      source: string;
      year: string[];
    };
  };
  aggregation_benchmark: {
    region: string;
    documents_available: number;
    // Recorded by the benchmark itself — the UI reads it instead of
    // hardcoding a thread count.
    parallel_workers: number;
    runs: {
      pattern: string;
      documents: number;
      seconds: number;
      // The benchmark ran every configuration twice; `seconds` is the
      // better (min) of the two passes.
      seconds_pass1?: number;
      seconds_pass2?: number;
      ms_per_document: number;
      aggregated_access_events: number;
    }[];
  };
}