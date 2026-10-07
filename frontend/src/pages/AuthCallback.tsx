// The hosted-UI redirect lands here with ?code=... The page exists
// only to complete the exchange and hand control back to the SPA;
// every hard failure renders its reason instead of looping.

import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { useLiveSystem } from "../hooks/useLiveSystem";
import { handleCallback } from "../services/auth";

export default function AuthCallback() {
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const { refreshDocuments } = useLiveSystem();

  // The exchange consumes the single-use PKCE record — this effect
  // must act once per page load, not per dependency change.
  const exchanged = useRef(false);

  useEffect(() => {
    if (exchanged.current) return;
    exchanged.current = true;

    // Read the query from the address bar directly: location.search
    // in a dep list re-runs the effect whenever the hook's callers
    // re-render, which re-invoked the single-use exchange.
    const params = new URLSearchParams(window.location.search);

    handleCallback(params)
      .then(() => {
        // The token session now exists; re-run the protected reads
        // that previously stopped at the authorizer's 401.
        refreshDocuments();
        setDone(true);
      })
      .catch((reason: unknown) => {
        setError(
          reason instanceof Error
            ? reason.message
            : "Sign-in could not be completed",
        );
      });
  }, [refreshDocuments]);

  if (error) {
    return (
      <section className="panel mt-4">
        <div className="panel-head">
          <div className="panel-title">Sign-in failed</div>
        </div>
        <p style={{ fontSize: 13, color: "var(--text-2)", lineHeight: 1.6 }}>
          {error} Close this tab and start again — the sign-in attempt
          cannot be retried with the same authorization code.
        </p>
      </section>
    );
  }

  if (done) return <Navigate to="/" replace />;

  return (
    <section className="panel mt-4">
      <p style={{ fontSize: 13, color: "var(--text-2)" }}>
        Completing sign-in…
      </p>
    </section>
  );
}