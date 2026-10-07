// Live backend state shared across the app: API reachability, the
// document list, aggregation runs, and the last-aggregation marker.
//
// The marker is stored in this browser only (localStorage) — the API
// exposes no "last aggregation" timestamp, so the top bar only ever
// shows an aggregation YOU ran from this browser, never a fabricated
// one.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import { api, isConfigured } from "../services/api";
import type { ApiDocument } from "../types";

export type ApiStatus =
  | "unconfigured"
  | "connecting"
  | "online"
  | "signed-out"
  | "offline";

const LAST_AGGREGATION_KEY = "storagelens.last-aggregation";

interface LastAggregation {
  timestamp: string;
  documentsProcessed: number;
}

interface LiveSystemState {
  status: ApiStatus;
  documents: ApiDocument[];
  error: string | null;
  lastAggregation: LastAggregation | null;
  refreshDocuments: () => void;
  recordAggregation: (documentsProcessed: number) => void;
}

const LiveSystemContext = createContext<LiveSystemState | null>(null);

function readLastAggregation(): LastAggregation | null {
  try {
    const raw = localStorage.getItem(LAST_AGGREGATION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as LastAggregation;
    return Number.isNaN(Date.parse(parsed.timestamp))
      ? null
      : parsed;
  } catch {
    return null;
  }
}

export function LiveSystemProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ApiStatus>(() =>
    isConfigured() ? "connecting" : "unconfigured",
  );
  const [documents, setDocuments] = useState<ApiDocument[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastAggregation, setLastAggregation] =
    useState<LastAggregation | null>(readLastAggregation);

  const refreshDocuments = useCallback(() => {
    if (!isConfigured()) {
      setStatus("unconfigured");
      return;
    }
    setStatus("connecting");
    setError(null);
    api
      .listDocuments()
      .then((result) => {
        setDocuments([...result.documents].sort((a, b) =>
          a.upload_timestamp < b.upload_timestamp ? 1 : -1,
        ));
        setStatus("online");
      })
      .catch((err: Error) => {
        setError(err.message);
        // Distinguish "not signed in" from "the backend is down":
        // the top bar and dashboards must not blame the network for
        // the authorizer's 401.
        setStatus(
          (err as { status?: number }).status === 401
            ? "signed-out"
            : "offline",
        );
      });
  }, []);

  useEffect(() => {
    refreshDocuments();
  }, [refreshDocuments]);

  const recordAggregation = useCallback(
    (documentsProcessed: number) => {
      const marker: LastAggregation = {
        timestamp: new Date().toISOString(),
        documentsProcessed,
      };
      try {
        localStorage.setItem(
          LAST_AGGREGATION_KEY,
          JSON.stringify(marker),
        );
      } catch {
        // Private-mode browsers may refuse storage — the marker is
        // per-browser convenience only; rendering proceeds without it.
      }
      setLastAggregation(marker);
      refreshDocuments();
    },
    [refreshDocuments],
  );

  const value = useMemo(
    () => ({
      status,
      documents,
      error,
      lastAggregation,
      refreshDocuments,
      recordAggregation,
    }),
    [status, documents, error, lastAggregation, refreshDocuments, recordAggregation],
  );

  return (
    <LiveSystemContext.Provider value={value}>
      {children}
    </LiveSystemContext.Provider>
  );
}

export function useLiveSystem(): LiveSystemState {
  const context = useContext(LiveSystemContext);
  if (!context) {
    throw new Error("useLiveSystem must be used within LiveSystemProvider");
  }
  return context;
}