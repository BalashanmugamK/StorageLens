// Onboarding — paste a bucket name, get a working import.
//
// The flow the docs describe in prose, made executable:
//   1. Check    — POST /imports/check answers, with bounded reads and
//                 nothing imported, whether this deployment can read
//                 the bucket, whether an inventory manifest is there,
//                 and whether anything under the log prefix parses
//                 like an S3 access log.
//   2. Inventory— POST /imports/inventory turns the manifest into
//                 document rows (idempotent per bucket+key).
//   3. Logs     — POST /imports/access-logs records the GETs.
//   4. Aggregate— POST /aggregates/run processes everything and the
//                 engine pages pick up from there.
//
// Nothing here hardcodes an account, region or bucket.

import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  CheckCircle2,
  CircleDashed,
  Compass,
} from "lucide-react";
import { api } from "../services/api";
import type {
  ApiAggregationResult,
  ApiImportAccessLogs,
  ApiImportCheck,
  ApiImportInventory,
} from "../types";
import { PageHeader, Spinner } from "../components/ui/atoms";

type InventoryResult = { ok?: ApiImportInventory; error?: string } | null;
type AccessResult = { ok?: ApiImportAccessLogs; error?: string } | null;

// One numbered step chip in the four-step header.
function StepChip({
  done,
  current,
  label,
}: {
  done: boolean;
  current: boolean;
  label: string;
}) {
  return (
    <span
      className="row"
      style={{
        gap: 6,
        alignItems: "center",
        fontSize: 12,
        padding: "4px 10px",
        borderRadius: 999,
        border: "1px solid var(--border)",
        background: current ? "var(--panel-2)" : "transparent",
        color: current ? "var(--text)" : "var(--text-3)",
      }}
    >
      {done ? (
        <CheckCircle2 size={13} style={{ color: "var(--accent, #3ddc97)" }} />
      ) : (
        <CircleDashed size={13} />
      )}
      {label}
    </span>
  );
}

function ResultLine({
  ok,
  children,
}: {
  ok: boolean;
  children: ReactNode;
}) {
  return (
    <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>
      <strong>{ok ? "Done." : "Failed."}</strong> {children}
    </div>
  );
}

export default function Onboarding() {
  const [bucket, setBucket] = useState("");
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<ApiImportCheck | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);

  const [manifestKey, setManifestKey] = useState("");
  const [logPrefix, setLogPrefix] = useState("");

  const [importing, setImporting] = useState<"inventory" | "logs" | "aggregate" | null>(null);
  const [inventoryResult, setInventoryResult] = useState<InventoryResult | null>(null);
  const [accessResult, setAccessResult] = useState<AccessResult | null>(null);
  const [aggregate, setAggregate] = useState<
    { result?: ApiAggregationResult; error?: string } | null
  >(null);

  const runCheck = async () => {
    if (!bucket.trim()) {
      setCheckError("A bucket name is required.");
      return;
    }
    setChecking(true);
    setCheckError(null);
    setCheck(null);
    try {
      const result = await api.checkImport({ bucket: bucket.trim() });
      setCheck(result);
      setManifestKey(
        result.inventory.mode === "checked" && result.inventory.key
          ? result.inventory.key
          : (result.inventory.candidates?.[0] ?? ""),
      );
      setLogPrefix(result.access_logs.prefix ?? "");
    } catch (err) {
      setCheckError(
        err instanceof Error ? err.message : "The preflight check failed.",
      );
    } finally {
      setChecking(false);
    }
  };

  const importInventory = async () => {
    if (!bucket.trim() || !manifestKey.trim()) {
      return;
    }
    setImporting("inventory");
    setInventoryResult(null);
    try {
      setInventoryResult({
        ok: await api.importInventory({ bucket: bucket.trim(), key: manifestKey.trim() }),
      });
    } catch (err) {
      setInventoryResult({
        error: err instanceof Error ? err.message : "The inventory import failed.",
      });
    } finally {
      setImporting(null);
    }
  };

  const importLogs = async () => {
    if (!bucket.trim()) {
      return;
    }
    setImporting("logs");
    setAccessResult(null);
    try {
      setAccessResult({
        ok: await api.importAccessLogs({
          bucket: bucket.trim(),
          prefix: logPrefix.trim() || undefined,
        }),
      });
    } catch (err) {
      setAccessResult({
        error: err instanceof Error ? err.message : "The access-log import failed.",
      });
    } finally {
      setImporting(null);
    }
  };

  const runAggregation = async () => {
    setImporting("aggregate");
    setAggregate(null);
    try {
      setAggregate({ result: await api.runAggregation() });
    } catch (err) {
      setAggregate({
        error: err instanceof Error ? err.message : "The aggregation run failed.",
      });
    } finally {
      setImporting(null);
    }
  };

  const reachable = check?.reachable === true;
  const manifestChosen = check !== null && manifestKey.trim().length > 0;
  const inventoryDone = inventoryResult?.ok !== undefined;
  const accessDone = accessResult?.ok !== undefined;
  const aggregateDone = aggregate?.result !== undefined;

  const inv = check?.inventory;
  const logs = check?.access_logs;

  return (
    <div className="page-in">
      <PageHeader
        title="Onboarding"
        subtitle="Connect storage the product never uploaded: paste a bucket name, verify it, import, aggregate. Nothing is changed in your bucket — every step only reads it."
      />

      <div
        className="row"
        style={{ gap: 8, margin: "0 4px 16px", flexWrap: "wrap" }}
      >
        <StepChip done={reachable} current={!check} label="1 · Check" />
        <StepChip done={inventoryDone} current={!inventoryDone} label="2 · Inventory" />
        <StepChip done={accessDone} current={!accessDone} label="3 · Access logs" />
        <StepChip done={aggregateDone} current={!aggregateDone} label="4 · Aggregate" />
      </div>

      {/* 1 — the preflight */}
      <section className="panel">
        <div className="panel-head">
          <div className="panel-title">1 · Check the bucket</div>
          <span className="muted" style={{ fontSize: 11 }}>
            reads only — imports nothing, changes nothing
          </span>
        </div>
        <div style={{ padding: "12px 16px", display: "grid", gap: 10 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <input
              className="input"
              style={{ flex: "1 1 260px" }}
              value={bucket}
              placeholder="the bucket whose storage you want priced"
              onChange={(e) => setBucket(e.target.value)}
              disabled={checking}
            />
            <button
              type="button"
              className="btn btn-primary"
              onClick={runCheck}
              disabled={checking}
            >
              {checking ? (
                <>
                  <Spinner size={13} /> Checking…
                </>
              ) : (
                "Check"
              )}
            </button>
          </div>

          {checkError && <ResultLine ok={false}>{checkError}</ResultLine>}

          {check && (
            <div
              style={{
                display: "grid",
                gap: 10,
                fontSize: 12.5,
                lineHeight: 1.6,
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                padding: "12px 14px",
                background: "var(--panel-2)",
              }}
            >
              <div>
                {reachable ? (
                  <strong style={{ color: "var(--accent, #3ddc97)" }}>
                    Readable.
                  </strong>
                ) : (
                  <strong style={{ color: "var(--danger, #c0392b)" }}>
                    Not readable.
                  </strong>
                )}{" "}
                {(check.notes ?? []).map((note) => (
                  <span key={note} className="doc-sub" style={{ display: "block" }}>
                    {note}
                  </span>
                ))}
              </div>

              {/* manifest status */}
              {reachable && (
                <div>
                  <strong>Inventory manifest:</strong>{" "}
                  {inv?.mode === "checked" &&
                    (inv.exists
                      ? `the given object exists${inv.columns_ok === true ? " and carries the columns the importer needs" : inv.columns_ok === false ? " but lacks required columns" : " (too large to sniff columns)"}`
                      : "the given object could not be read")}
                  {inv?.mode === "discovered" &&
                    (inv.candidates?.length
                      ? `${inv.candidates.length} schema.csv candidate${inv.candidates.length === 1 ? "" : "s"} found in the first listing pages`
                      : "no schema.csv found in the first listing pages")}
                  {!inv?.mode && "not checked"}
                  {inv?.missing_columns?.length ? (
                    <span className="doc-sub" style={{ display: "block" }}>
                      missing: {inv.missing_columns.join(", ")}
                    </span>
                  ) : null}
                  {inv?.notes.map((note) => (
                    <span key={note} className="doc-sub" style={{ display: "block" }}>
                      {note}
                    </span>
                  ))}
                </div>
              )}

              {/* log status */}
              {reachable && (
                <div>
                  <strong>Access logs:</strong>{" "}
                  {typeof logs?.files_found === "number"
                    ? `${logs.files_found.toLocaleString()} object${logs.files_found === 1 ? "" : "s"} under ${logs.prefix ? `“${logs.prefix}”` : "the bucket root"}${logs.listing_truncated ? " (listing capped)" : ""}`
                    : "not checked"}
                  {typeof logs?.sample_parse_success === "number" && (
                    <span className="doc-sub" style={{ display: "block" }}>
                      sampled {(logs.sampled_files ?? []).length} object{((logs.sampled_files ?? []).length === 1 ? "" : "s")}: {logs.sample_parse_success} parseable line{logs.sample_parse_success === 1 ? "" : "s"}
                    </span>
                  )}
                  {logs?.notes.map((note) => (
                    <span key={note} className="doc-sub" style={{ display: "block" }}>
                      {note}
                    </span>
                  ))}
                </div>
              )}

              <div className="doc-sub">
                {check.basis}
              </div>
            </div>
          )}
        </div>
      </section>

      {/* 2 — inventory import */}
      <section className="panel mt-3" style={{ opacity: reachable ? 1 : 0.55 }}>
        <div className="panel-head">
          <div className="panel-title">2 · Import the inventory</div>
          {check?.ready ? (
            <span className="badge badge-live">Preflight passed</span>
          ) : reachable ? (
            <span className="badge">Preflight incomplete</span>
          ) : null}
        </div>
        <div style={{ padding: "12px 16px", display: "grid", gap: 10 }}>
          {check?.inventory.candidates?.length ? (
            <div
              style={{
                display: "grid",
                gap: 6,
                fontSize: 12.5,
                gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              }}
              role="radiogroup"
              aria-label="Discovered manifest candidates"
            >
              {check.inventory.candidates.map((candidate) => (
                <label
                  key={candidate}
                  className="row mono"
                  style={{ gap: 6, fontSize: 11.5, alignItems: "center", cursor: "pointer" }}
                >
                  <input
                    type="radio"
                    name="manifest"
                    checked={manifestKey === candidate}
                    onChange={() => setManifestKey(candidate)}
                    disabled={importing !== null}
                  />
                  <span style={{ wordBreak: "break-all" }}>{candidate}</span>
                </label>
              ))}
            </div>
          ) : reachable ? (
            <label style={{ fontSize: 12, display: "grid", gap: 4 }}>
              Manifest object key (schema.csv or schema.csv.gz)
              <input
                className="input"
                value={manifestKey}
                placeholder="inventory/site/Hive/schema.csv"
                onChange={(e) => setManifestKey(e.target.value)}
                disabled={importing !== null || !reachable}
              />
            </label>
          ) : null}

          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={importInventory}
              disabled={!manifestChosen || importing !== null}
            >
              {importing === "inventory" ? "Importing…" : "Run inventory import"}
            </button>
            <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
              one document row per object · deterministic ids, re-importing overwrites
            </span>
          </div>

          {inventoryResult?.ok && (
            <ResultLine ok>
              Imported {inventoryResult.ok.documents_imported.toLocaleString()} documents.
              {inventoryResult.ok.documents_skipped_unsupported_class > 0 &&
                ` ${inventoryResult.ok.documents_skipped_unsupported_class.toLocaleString()} skipped with unsupported storage classes.`}
              {inventoryResult.ok.documents_skipped_invalid > 0 &&
                ` ${inventoryResult.ok.documents_skipped_invalid.toLocaleString()} skipped as invalid rows.`}
              {inventoryResult.ok.truncated &&
                ` TRUNCATED at the ${inventoryResult.ok.row_cap.toLocaleString()} row cap — split the manifest and import the rest.`}
            </ResultLine>
          )}
          {inventoryResult?.error && <ResultLine ok={false}>{inventoryResult.error}</ResultLine>}
        </div>
      </section>

      {/* 3 — access-log import */}
      <section className="panel mt-3" style={{ opacity: reachable ? 1 : 0.55 }}>
        <div className="panel-head">
          <div className="panel-title">3 · Import the access logs</div>
          <span className="muted" style={{ fontSize: 11 }}>
            optional — but recency features need it
          </span>
        </div>
        <div style={{ padding: "12px 16px", display: "grid", gap: 10 }}>
          <label style={{ fontSize: 12, display: "grid", gap: 4 }}>
            Prefix holding the S3 server access logs (empty = whole bucket)
            <input
              className="input"
              value={logPrefix}
              placeholder="access-logs/"
              onChange={(e) => setLogPrefix(e.target.value)}
              disabled={importing !== null || !reachable}
            />
          </label>

          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={importLogs}
              disabled={!reachable || importing !== null}
            >
              {importing === "logs" ? "Importing…" : "Run access-log import"}
            </button>
            <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
              successful GETs become DOWNLOAD events with the logs' own timestamps
            </span>
          </div>

          {accessResult?.ok && (
            <ResultLine ok>
              Recorded {accessResult.ok.events_recorded.toLocaleString()} access events from{" "}
              {accessResult.ok.files_scanned} log file{accessResult.ok.files_scanned === 1 ? "" : "s"}.
              {accessResult.ok.files_failed.length > 0 &&
                ` ${accessResult.ok.files_failed.length} file${accessResult.ok.files_failed.length === 1 ? "" : "s"} failed to parse.`}
              {accessResult.ok.truncated &&
                ` TRUNCATED at the ${accessResult.ok.file_cap} file cap — run again for the next batch.`}
            </ResultLine>
          )}
          {accessResult?.error && <ResultLine ok={false}>{accessResult.error}</ResultLine>}
        </div>
      </section>

      {/* 4 — aggregation */}
      <section className="panel mt-3" style={{ opacity: reachable ? 1 : 0.55 }}>
        <div className="panel-head">
          <div className="panel-title">4 · Aggregate and go</div>
        </div>
        <div style={{ padding: "12px 16px", display: "grid", gap: 10 }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={runAggregation}
              disabled={!reachable || importing !== null}
            >
              {importing === "aggregate" ? "Aggregating…" : "Run aggregation"}
            </button>
            <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
              builds the engine's feature rows — after this, Optimization and Approvals treat your bucket like any other
            </span>
          </div>

          {aggregate?.result && (
            <ResultLine ok>
              Processed {aggregate.result.documents_processed.toLocaleString()} documents.{" "}
              <Link to="/optimization">Open the fleet projection →</Link>
            </ResultLine>
          )}
          {aggregate?.error && <ResultLine ok={false}>{aggregate.error}</ResultLine>}
        </div>
      </section>

      {/* The prerequisite prose, condensed from the repo README. */}
      <section className="panel mt-3">
        <div className="panel-head">
          <div className="panel-title">If the check came back empty or unreadable</div>
        </div>
        <div style={{ padding: "14px 16px 16px", fontSize: 12.5, lineHeight: 1.7 }}>
          <Compass size={15} style={{ verticalAlign: "-2px", marginRight: 6 }} />
          The check only sees what this deployment's role can read. To give it
          something to find, on the <strong>source bucket</strong> enable{" "}
          <a href="https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-inventory.html" target="_blank" rel="noreferrer">S3 Inventory</a>{" "}
          (CSV, optionally Gzip — the wizard finds its <span className="mono">schema.csv</span>) and{" "}
          <a href="https://docs.aws.amazon.com/AmazonS3/latest/userguide/ServerLogs.html" target="_blank" rel="noreferrer">server access logging</a>{" "}
          to a prefix. Then re-run the check. Both imports are idempotent and
          read-only against your bucket. The long-form walkthrough lives on the{" "}
          <Link to="/guide">Guide</Link> page.
        </div>
      </section>
    </div>
  );
}