// Download button: asks the API for a presigned GET URL (which also
// records a DOWNLOAD access event server-side), then fetches the
// object from S3. Errors stay inline — no stack traces.

import { useState } from "react";
import { Download } from "lucide-react";
import { api } from "../../services/api";

export function DownloadButton({
  documentId,
  small = true,
  onDone,
}: {
  documentId: string;
  small?: boolean;
  onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handle = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.downloadDocument(documentId);
      const anchor = document.createElement("a");
      anchor.href = result.download_url;
      anchor.rel = "noopener";
      anchor.download = "";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      onDone?.();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="row" style={{ gap: 6 }}>
      <button
        type="button"
        className={`btn btn-sm${small ? "" : ""}`}
        onClick={handle}
        disabled={busy}
        title="Fetches a presigned URL — this download is logged as an access event"
      >
        <Download size={13} />
        {busy ? "Preparing…" : "Download"}
      </button>
      {error && (
        <span style={{ color: "var(--danger)", fontSize: 11.5 }}>{error}</span>
      )}
    </span>
  );
}