// Upload flow: the backend mints a presigned S3 PUT URL and the file
// bytes go straight to S3 — API Gateway never carries the payload.

import { useEffect, useRef, useState } from "react";
import { Check, CloudUpload, FileUp, X } from "lucide-react";
import { api } from "../../services/api";
import { formatBytes } from "../../utils/format";

type Phase = "choose" | "creating" | "uploading" | "done" | "error";

const ACCEPTED = [".pdf", ".docx", ".txt", ".md"];

export function UploadDialog({
  open,
  onClose,
  onUploaded,
}: {
  open: boolean;
  onClose: () => void;
  onUploaded: (documentId: string) => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("choose");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [uploadedId, setUploadedId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setFile(null);
      setPhase("choose");
      setProgress(0);
      setError(null);
      setUploadedId(null);
    }
  }, [open]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && open && phase !== "creating" && phase !== "uploading") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, phase, onClose]);

  if (!open) return null;

  const validateAndSet = (candidate: File) => {
    const lower = candidate.name.toLowerCase();
    if (!ACCEPTED.some((ext) => lower.endsWith(ext))) {
      setError(`Unsupported file type. Allowed: ${ACCEPTED.join(", ")}`);
      setPhase("error");
      return;
    }
    setError(null);
    setPhase("choose");
    setFile(candidate);
  };

  const start = async () => {
    if (!file) return;
    setPhase("creating");
    setProgress(0);
    setError(null);
    try {
      const created = await api.createDocument({
        file_name: file.name,
        file_size: file.size,
        content_type:
          file.type ||
          (file.name.toLowerCase().endsWith(".pdf")
            ? "application/pdf"
            : "application/octet-stream"),
      });
      setPhase("uploading");
      await api.uploadToS3(
        created.upload_url,
        file.type || "application/octet-stream",
        file,
        (fraction) => setProgress(fraction),
      );
      setUploadedId(created.document_id);
      setPhase("done");
      onUploaded(created.document_id);
    } catch (err) {
      setError((err as Error).message);
      setPhase("error");
    }
  };

  return (
    <div
      className="modal-scrim"
      onClick={(e) => {
        if (e.target === e.currentTarget && phase !== "creating" && phase !== "uploading") {
          onClose();
        }
      }}
    >
      <div
        className="modal panel panel-pad"
        role="dialog"
        aria-modal="true"
        aria-label="Upload a document"
      >
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
          <h2 style={{ fontSize: 16 }}>Upload a document</h2>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={onClose}
            aria-label="Close upload dialog"
            disabled={phase === "creating" || phase === "uploading"}
          >
            <X size={15} />
          </button>
        </div>
        <p style={{ color: "var(--text-2)", fontSize: 12.5 }}>
          Metadata is registered with the documents API; the file itself is
          uploaded directly to S3 with a presigned URL.
        </p>

        {phase === "done" ? (
          <div className="state-block" style={{ padding: "28px 0 12px" }}>
            <div
              className="state-icon"
              style={{ color: "var(--accent-strong)", borderColor: "var(--accent-border)" }}
            >
              <Check size={20} />
            </div>
            <div className="state-title">Upload complete</div>
            {file && (
              <p className="state-desc">
                {file.name} ({formatBytes(file.size)}) is stored in S3
                Standard.
              </p>
            )}
            <div className="state-actions">
              <button type="button" className="btn" onClick={() => setPhase("choose")}>
                Upload another
              </button>
              <button type="button" className="btn btn-primary" onClick={onClose}>
                Done
              </button>
            </div>
            {uploadedId && (
              <p className="mt-2 mono muted" style={{ wordBreak: "break-all" }}>
                document_id: {uploadedId}
              </p>
            )}
          </div>
        ) : (
          <>
            <div
              className={`dropzone mt-2${dragOver ? " over" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                const dropped = e.dataTransfer.files?.[0];
                if (dropped) validateAndSet(dropped);
              }}
            >
              <FileUp size={22} strokeWidth={1.5} style={{ color: "var(--text-3)" }} />
              {file ? (
                <div style={{ marginTop: 10 }}>
                  <div style={{ color: "var(--text)", fontWeight: 550, fontSize: 13.5 }}>
                    {file.name}
                  </div>
                  <div className="muted tnum" style={{ fontSize: 12 }}>
                    {formatBytes(file.size)}
                  </div>
                </div>
              ) : (
                <>
                  <div style={{ marginTop: 10, fontSize: 13.5 }}>
                    Drop a legal document here
                  </div>
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                    PDF, DOCX, TXT · max 5 GB
                  </div>
                </>
              )}
              <div className="state-actions">
                <button
                  type="button"
                  className="btn"
                  onClick={() => inputRef.current?.click()}
                >
                  Browse files
                </button>
                {file && (
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={start}
                    disabled={phase !== "choose"}
                  >
                    {phase === "choose" && <CloudUpload size={14} />}
                    {phase === "creating" && <span className="pulse">Registering…</span>}
                    {phase === "uploading" && `Uploading ${Math.round(progress * 100)}%`}
                    {phase === "error" && "Retry upload"}
                    {phase === "choose" && "Upload"}
                  </button>
                )}
              </div>
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPTED.join(",")}
                hidden
                onChange={(e) => {
                  const picked = e.target.files?.[0];
                  if (picked) validateAndSet(picked);
                  e.target.value = "";
                }}
              />
            </div>

            {(phase === "creating" || phase === "uploading") && (
              <div className="progress mt-2" role="status" aria-label="Upload progress">
                <div
                  className="progress-bar"
                  style={{ width: `${Math.max(progress * 100, 6)}%` }}
                />
              </div>
            )}

            {error && (
              <p
                role="alert"
                style={{ color: "var(--danger)", fontSize: 12.5, marginTop: 12 }}
              >
                {error}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}