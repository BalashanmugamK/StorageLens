// Centralized API client for the StorageLens live backend.
//
// The base URL is read once from VITE_API_BASE_URL (see .env.example).
// Nothing here ever hardcodes an AWS account, region, resource name or
// API Gateway URL — the deployed stack generates all of them.

import type {
  ApiAccessStats,
  ApiAggregationResult,
  ApiAggregationStats,
  ApiCreateDocument,
  ApiDecision,
  ApiDecisionList,
  ApiDocument,
  ApiDownload,
  ApiExecuteResult,
  ApiFleetProjection,
  ApiImportAccessLogs,
  ApiImportCheck,
  ApiImportInventory,
  ApiLedger,
  ApiOnCallCoverage,
  ApiOnCallDeleteResult,
  ApiOnCallMe,
  ApiOnCallNotificationList,
  ApiOnCallShift,
  ApiOnCallShiftInput,
  ApiOnCallShiftList,
  ApiOnCallSyncResult,
  ApiProposeBatchResult,
  ApiReconciliationReport,
  ApiTransitionResult,
} from "../types";
import { API_BASE_URL, isConfigured } from "./apiBase";
import { forceRefresh, getIdToken } from "./auth";

export { API_BASE_URL, isConfigured };

export class ApiError extends Error {
  status: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status ?? 0;
  }
}

// Every route sits behind the Cognito JWT authorizer except
// GET /config (fetched by the auth service directly), so requests
// carry the session's Bearer token when one exists.
//
// On a 401 the request retries once AFTER attempting a token
// refresh — a mid-session expiry should be transparent, while a
// genuinely dead session surfaces as an ApiError(401) the caller
// renders as a sign-in requirement (no redirect loop).
async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  if (!API_BASE_URL) {
    throw new ApiError(
      "API base URL is not configured. Set VITE_API_BASE_URL in your .env file.",
    );
  }

  const { timeoutMs = 120_000, ...fetchInit } = init ?? {};

  async function attempt(): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const headers = new Headers(fetchInit.headers);
      const token = await getIdToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);

      return await fetch(`${API_BASE_URL}${path}`, {
        ...fetchInit,
        headers,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  let response: Response;

  try {
    response = await attempt();

    if (response.status === 401) {
      // One renewal attempt: a mid-session expiry stays transparent;
      // a genuinely dead session surfaces as an ApiError(401) the
      // caller renders as a sign-in requirement (no redirect loop).
      if (await forceRefresh()) {
        response = await attempt();
      }
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("Request timed out before the API responded.");
    }
    throw new ApiError("Unable to reach the optimization API.");
  }

  if (!response.ok) {
    if (response.status === 401) {
      // Past the refresh retry: the authorizer rejected the final
      // attempt, i.e. genuinely not signed in.
      throw new ApiError(
        "Not signed in — use the Sign in control in the top bar.",
        401,
      );
    }

    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { error?: string };
      if (body?.error) message = body.error;
    } catch {
      // non-JSON error body — keep the generic message
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  listDocuments(): Promise<{ documents: ApiDocument[] }> {
    return request("/documents");
  },

  getDocument(documentId: string): Promise<ApiDocument> {
    return request(`/documents/${encodeURIComponent(documentId)}`);
  },

  getAccessStats(documentId: string): Promise<ApiAccessStats> {
    return request(
      `/documents/${encodeURIComponent(documentId)}/access-stats`,
    );
  },

  createDocument(body: {
    file_name: string;
    file_size: number;
    content_type: string;
  }): Promise<ApiCreateDocument> {
    return request("/documents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  // The backend returns a presigned S3 PUT URL — the file bytes go
  // straight to S3 and never pass through API Gateway.
  async uploadToS3(
    uploadUrl: string,
    contentType: string,
    file: File,
    onProgress?: (fraction: number) => void,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", uploadUrl);
      xhr.setRequestHeader("Content-Type", contentType);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(event.loaded / event.total);
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve();
        } else {
          reject(new ApiError(`S3 upload failed (${xhr.status})`, xhr.status));
        }
      };
      xhr.onerror = () => reject(new ApiError("S3 upload failed."));
      xhr.send(file);
    });
  },

  downloadDocument(documentId: string): Promise<ApiDownload> {
    return request(
      `/documents/${encodeURIComponent(documentId)}/download`,
      { timeoutMs: 60_000 },
    );
  },

  runAggregation(): Promise<ApiAggregationResult> {
    return request("/aggregates/run", { method: "POST", timeoutMs: 300_000 });
  },

  // Live state of the aggregation pipeline: coverage of the
  // aggregates table, last run, storage-class distribution.
  getAggregationStats(): Promise<ApiAggregationStats> {
    return request("/aggregates/stats");
  },

  // The engine run over every LIVE aggregate: modeled 12-month
  // baseline vs optimized fleet cost. ?policy=A|B, default B.
  getFleetProjection(policy?: "A" | "B"): Promise<ApiFleetProjection> {
    return request(
      `/fleet/projection${policy ? `?policy=${policy}` : ""}`,
      { timeoutMs: 120_000 },
    );
  },

  /* ---- Decisions: the write half of the loop ---- */

  // Asks the backend to re-run the real engine over the document's
  // latest aggregate and record a proposed migration (409 when the
  // engine recommends keeping the document in place, or another
  // decision is already open for it).
  createDecision(documentId: string): Promise<ApiDecision> {
    return request("/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: documentId }),
      // Engine re-run against the aggregate + S3 lookup stays well
      // under this; keep the UI's spinner honest either way.
      timeoutMs: 120_000,
    });
  },

  listDecisions(): Promise<ApiDecisionList> {
    return request("/decisions");
  },

  // Records proposals for every live aggregate the engine would
  // migrate, capped per call (repeat the call to keep proposing).
  proposeDecisionBatch(options?: { max?: number }): Promise<ApiProposeBatchResult> {
    return request("/decisions/propose-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max: options?.max }),
      timeoutMs: 120_000,
    });
  },

  getDecision(decisionId: string): Promise<ApiDecision> {
    return request(
      `/decisions/${encodeURIComponent(decisionId)}`,
    );
  },

  approveDecision(decisionId: string): Promise<ApiTransitionResult> {
    return request(
      `/decisions/${encodeURIComponent(decisionId)}/approve`,
      { method: "POST", timeoutMs: 30_000 },
    );
  },

  rejectDecision(decisionId: string): Promise<ApiTransitionResult> {
    return request(
      `/decisions/${encodeURIComponent(decisionId)}/reject`,
      { method: "POST", timeoutMs: 30_000 },
    );
  },

  // Approve → execute → verify is irreversible (a real S3 storage-class
  // transition); execution is POST-only on the backend precisely so a
  // prefetched GET can never fire it.
  executeDecision(decisionId: string): Promise<ApiExecuteResult> {
    return request(
      `/decisions/${encodeURIComponent(decisionId)}/execute`,
      { method: "POST", timeoutMs: 180_000 },
    );
  },

  getLedger(): Promise<ApiLedger> {
    return request("/ledger", { timeoutMs: 60_000 });
  },

  /* ---- Reconciliation: the model vs the actual bill ---- */

  // Latest recorded reconciliation report; 404 until one has run
  // (the caller renders an empty state with the Run control).
  getReconciliationResult(): Promise<ApiReconciliationReport> {
    return request("/reconciliation/result", { timeoutMs: 60_000 });
  },

  // Queries Cost Explorer for complete months of S3 usage-type
  // charges, predicts the same months from the live aggregate
  // snapshot, records and returns the report.
  runReconciliation(months?: number): Promise<ApiReconciliationReport> {
    return request("/reconciliation/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ months }),
      timeoutMs: 120_000,
    });
  },

  /* ---- Ingest: data that was never uploaded through this product ---- */

  // Onboarding preflight: bounded reads that answer "will an import
  // work on this bucket?" — reachability, manifest presence/columns,
  // access-log presence/format — without importing anything.
  checkImport(body: { bucket: string; key?: string; prefix?: string }): Promise<ApiImportCheck> {
    return request("/imports/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 60_000,
    });
  },

  // One S3 Inventory manifest (CSV or .csv.gz) becomes document rows.
  // Idempotent per (bucket, key): re-importing overwrites the same ids.
  importInventory(body: {
    bucket: string;
    key: string;
    document_state?: string;
  }): Promise<ApiImportInventory> {
    return request("/imports/inventory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 300_000,
    });
  },

  // Successful GETs in S3 server access logs under a prefix become
  // real DOWNLOAD access events with the logs' own timestamps.
  importAccessLogs(body: {
    bucket: string;
    prefix?: string;
  }): Promise<ApiImportAccessLogs> {
    return request("/imports/access-logs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 300_000,
    });
  },

  /* ---- On-Call: shift schedule + PagerDuty sync + paging audit ---- */

  // Who the caller is on this backend (email/sub) and whether the
  // authorizer grants the Approver role that gates the write routes.
  getOnCallMe(): Promise<ApiOnCallMe> {
    return request("/oncall/me");
  },

  // The live coverage resolution over the schedule: who covers now,
  // per role, plus any overlapping shifts (a UI warning, not an error).
  getOnCallCurrent(): Promise<ApiOnCallCoverage> {
    return request("/oncall/current");
  },

  // Shift listing: limit pages via next_token (absent when exhausted);
  // engineer/role/from/to narrow the schedule query. Writes need the
  // Approver role — a non-approver gets a 403 the caller renders.
  listOnCallShifts(options?: {
    limit?: number;
    next_token?: string;
    engineer?: string;
    role?: string;
    from?: string;
    to?: string;
  }): Promise<ApiOnCallShiftList> {
    const params = new URLSearchParams();
    if (options?.limit !== undefined) params.set("limit", String(options.limit));
    if (options?.next_token) params.set("next_token", options.next_token);
    if (options?.engineer) params.set("engineer", options.engineer);
    if (options?.role) params.set("role", options.role);
    if (options?.from) params.set("from", options.from);
    if (options?.to) params.set("to", options.to);
    const query = params.toString();
    return request(`/oncall/shifts${query ? `?${query}` : ""}`);
  },

  createOnCallShift(
    body: ApiOnCallShiftInput,
  ): Promise<{ shift: ApiOnCallShift }> {
    return request("/oncall/shifts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 60_000,
    });
  },

  updateOnCallShift(
    shiftId: string,
    body: ApiOnCallShiftInput,
  ): Promise<{ shift: ApiOnCallShift }> {
    return request(
      `/oncall/shifts/${encodeURIComponent(shiftId)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        timeoutMs: 60_000,
      },
    );
  },

  // Delete is 200 { "deleted": id } — but an empty proxy body must be
  // tolerated, so a 2xx response that fails to parse becomes the same
  // shape (a non-ok status still throws the ApiError from request).
  async deleteOnCallShift(shiftId: string): Promise<ApiOnCallDeleteResult> {
    try {
      return await request<ApiOnCallDeleteResult>(
        `/oncall/shifts/${encodeURIComponent(shiftId)}`,
        { method: "DELETE", timeoutMs: 60_000 },
      );
    } catch (error) {
      if (error instanceof SyntaxError) return { deleted: shiftId };
      throw error;
    }
  },

  // Manual PagerDuty schedule reconcile: diffs the next 30 days of
  // overrides against the recorded future shifts and converges.
  syncOnCall(): Promise<ApiOnCallSyncResult> {
    return request("/oncall/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeoutMs: 120_000,
    });
  },

  // Paging audit history, newest first; alarm filters to one alarm.
  // limit pages via next_token (absent when exhausted).
  listOnCallNotifications(options?: {
    limit?: number;
    next_token?: string;
    alarm?: string;
  }): Promise<ApiOnCallNotificationList> {
    const params = new URLSearchParams();
    if (options?.limit !== undefined) params.set("limit", String(options.limit));
    if (options?.next_token) params.set("next_token", options.next_token);
    if (options?.alarm) params.set("alarm", options.alarm);
    const query = params.toString();
    return request(`/oncall/notifications${query ? `?${query}` : ""}`);
  },
};