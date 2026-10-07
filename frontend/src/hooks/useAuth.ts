// React binding for the auth service: one hook, one session state.

import { useEffect, useState } from "react";
import type { Session } from "../services/authLogic";
import {
  currentSession,
  loadAuthConfig,
  subscribeAuth,
} from "../services/auth";

export interface AuthStatus {
  /** Unknown until /config resolves; false when the backend has no
      pool wired (local/test builds) — those stay unauthenticated. */
  required: boolean | "unknown";
  session: Session | null;
}

export function useAuthStatus(): AuthStatus {
  const [status, setStatus] = useState<AuthStatus>({
    required: "unknown",
    session: currentSession(),
  });

  useEffect(() => {
    let active = true;

    loadAuthConfig().then((config) => {
      if (active) {
        setStatus({
          required: config !== null,
          session: currentSession(),
        });
      }
    });

    const unsubscribe = subscribeAuth((session) => {
      setStatus((prev) => ({
        ...prev,
        session,
        required: prev.required === "unknown" ? "unknown" : true,
      }));
    });

    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return status;
}