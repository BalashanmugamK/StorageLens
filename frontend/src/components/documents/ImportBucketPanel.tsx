// "Import from bucket" — the bridge to data that was never uploaded
// through this product: an S3 Inventory manifest and S3 server access
// logs against a bucket the customer already owns. Both calls are
// idempotent and every result is reported verbatim (imported, skipped
// with reasons, truncated, basis).

import { useState } from "react";
import { api, ApiError } from "../../services/api";
import type { ApiImportAccessLogs, ApiImportInventory } from "../../types";

type ImportMode = "inventory" | "access-logs";

type DoneNotice =
  | { kind: "inventory"; result: ApiImportInventory }
  | { kind: "access-logs"; result: ApiImportAccessLogs };

export function ImportBucketPanel({
  disabled,
  onImported,
}: {
  disabled: boolean;
  onImported?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<ImportMode>("inventory");
  const [bucket, setBucket] = useState("");
  const [key, setKey] = useState("");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<DoneNotice | null>(null);

  const start = async () => {
    if (!bucket.trim()) {
      setError("A bucket name is required for both import modes.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      if (mode === "inventory") {
        if (!key.trim()) {
          setError("The inventory manifest object key is required (e.g. inventory/site_1/Hive/schema.csv or .csv.gz).");
          return;
        }
        const result = await api.importInventory({
          bucket: bucket.trim(),
          key: key.trim(),
        });
        setDone({ kind: "inventory", result });
        onImported?.();
      } else {
        const result = await api.importAccessLogs({
          bucket: bucket.trim(),
          prefix: prefix.trim() || undefined,
        });
        setDone({ kind: "access-logs", result });
      }
    } catch (err) {
      setDone(null);
      setError(
        err instanceof ApiError
          ? err.message
          : "The import could not be started.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        className="btn"
        onClick={() => setOpen(true)}
        disabled={disabled}
        title={
          disabled
            ? "Import needs a reachable backend"
            : "Bring an existing bucket's inventory and access logs into the pipeline"
        }
      >
        Import from bucket…
      </button>
    );
  }

  return (
    <section className="panel mt-2" aria-label="Import from bucket">
      <div className="panel-head">
        <div className="panel-title">Import from bucket</div>
        <div className="row">
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={() => setOpen(false)}
          >
            Close
          </button>
        </div>
      </div>

      <div style={{ padding: "12px 16px", display: "grid", gap: 10 }}>
        {/* Mode: Inventory vs access logs */}
        <div className="seg" role="tablist">
          <button
            type="button"
            className={mode === "inventory" ? "on" : undefined}
            role="tab"
            aria-selected={mode === "inventory"}
            onClick={() => setMode("inventory")}
          >
            Inventory manifest
          </button>
          <button
            type="button"
            className={mode === "access-logs" ? "on" : undefined}
            role="tab"
            aria-selected={mode === "access-logs"}
            onClick={() => setMode("access-logs")}
          >
            Access logs
          </button>
        </div>

        <label style={{ fontSize: 12, display: "grid", gap: 4 }}>
          Bucket to read from
          <input
            className="input"
            value={bucket}
            placeholder="customer-archive-eu"
            onChange={(e) => setBucket(e.target.value)}
            disabled={busy}
          />
        </label>

        {mode === "inventory" ? (
          <label style={{ fontSize: 12, display: "grid", gap: 4 }}>
            Inventory CSV object key (schema.csv or schema.csv.gz)
            <input
              className="input"
              value={key}
              placeholder="inventory/site_1/Hive/schema.csv"
              onChange={(e) => setKey(e.target.value)}
              disabled={busy}
            />
          </label>
        ) : (
          <label style={{ fontSize: 12, display: "grid", gap: 4 }}>
            Prefix holding S3 server access logs (empty = whole bucket)
            <input
              className="input"
              value={prefix}
              placeholder="access-logs/"
              onChange={(e) => setPrefix(e.target.value)}
              disabled={busy}
            />
          </label>
        )}

        <div className="row" style={{ alignItems: "center" }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={start}
            disabled={busy}
          >
            {busy ? "Importing…" : "Run import"}
          </button>
          <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
            {mode === "inventory"
              ? "Creates one document row per object — ids are deterministic, so re-importing overwrites instead of duplicating."
              : "Successful object GETs become DOWNLOAD events with the logs' own timestamps — the pipeline's access features read these."}
          </span>
        </div>

        {error && (
          <p style={{ fontSize: 12.5, color: "var(--danger, #c0392b)" }}>
            {error}
          </p>
        )}

        {done && done.kind === "inventory" && (
          <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>
            <strong>
              Imported {done.result.documents_imported} documents.
            </strong>{" "}
            {done.result.documents_skipped_unsupported_class > 0 &&
              `Skipped ${done.result.documents_skipped_unsupported_class} with unsupported storage classes (${done.result.unsupported_class_examples.join(", ")}).`}
            {done.result.documents_skipped_invalid > 0 &&
              ` Skipped ${done.result.documents_skipped_invalid} rows without a usable object key/size.`}
            {done.result.truncated &&
              ` TRUNCATED at the ${done.result.row_cap.toLocaleString()} row cap — split the manifest and import the parts.`}
            <div style={{ color: "var(--text-3)", marginTop: 4 }}>
              {done.result.basis}
            </div>
          </div>
        )}

        {done && done.kind === "access-logs" && (
          <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>
            <strong>Recorded {done.result.events_recorded} access events</strong>{" "}
            from {done.result.files_scanned} log files.
            {done.result.files_failed.length > 0 &&
              ` ${done.result.files_failed.length} files failed to parse.`}
            {done.result.truncated &&
              ` TRUNCATED at the ${done.result.file_cap.toLocaleString()} file cap — call again for the next batch.`}
            <div style={{ color: "var(--text-3)", marginTop: 4 }}>
              {done.result.basis}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}