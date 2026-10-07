// The browser side of sign-in: fetches the public /config bootstrap,
// runs the hosted-UI redirect, stores the token session in
// sessionStorage, and hands a Bearer token to the API client.
//
// All network/window/storage effects live HERE; the flow's logic
// (PKCE shapes, URL building, token parsing) is in authLogic.ts, and
// the two are tested separately.
//
// sessionStorage (not localStorage) intentionally: a stolen token
// should not outlive the tab that used it. The refresh token keeps
// one tab's session alive; a new tab just signs in again.

import { API_BASE_URL, isConfigured } from "./apiBase";
import {
  type AuthConfig,
  type Session,
  authorizeUrl,
  createCodeVerifier,
  logoutUrl,
  refreshRequest,
  sessionFromTokenResponse,
  tokenExchangeRequest,
  isSessionUsable,
} from "./authLogic";

const SESSION_KEY = "storagelens.session";
const PKCE_KEY = "storagelens.pkce";

export type AuthRequired = boolean | "unknown";

let cachedConfig: AuthConfig | null = null;
let configFetch: Promise<AuthConfig | null> | null = null;

/** True when the deployed backend requires sign-in. "unknown" before
    the public /config call resolves; null afterwards means it does
    not (no pool wired — local/test builds). */
export async function loadAuthConfig(): Promise<AuthConfig | null> {
  if (cachedConfig) return cachedConfig;
  if (!isConfigured()) return null;

  configFetch ??= (async () => {
    const response = await fetch(`${API_BASE_URL}/config`);
    if (!response.ok) throw new Error("Failed to load sign-in config");
    const payload = (await response.json()) as {
      auth: AuthConfig | null;
    };
    cachedConfig = payload.auth;
    return cachedConfig;
  })();

  return configFetch;
}

export function authConfigKnown(): boolean {
  return cachedConfig !== null;
}

function readSession(): Session | null {
  const raw = sessionStorage.getItem(SESSION_KEY);
  if (!raw) return null;

  try {
    return JSON.parse(raw) as Session;
  } catch {
    sessionStorage.removeItem(SESSION_KEY);
    return null;
  }
}

function writeSession(session: Session | null): void {
  if (session) {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } else {
    sessionStorage.removeItem(SESSION_KEY);
  }
}

type Listener = (session: Session | null) => void;

const listeners = new Set<Listener>();

function emit(session: Session | null): void {
  for (const listener of listeners) listener(session);
}

export function subscribeAuth(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function currentSession(): Session | null {
  return readSession();
}

export function signedIn(): boolean {
  return isSessionUsable(readSession());
}

/** Kick the hosted-UI sign-in redirect. Verifier + state are stored
    under PKCE_KEY so handleCallback can bind the exchange. */
export async function signIn(): Promise<void> {
  const config = await loadAuthConfig();

  if (!config) throw new Error("This backend does not require sign-in");

  const verifier = createCodeVerifier();
  const state = crypto.randomUUID();

  sessionStorage.setItem(
    PKCE_KEY,
    JSON.stringify({
      verifier,
      redirect_uri: window.location.origin,
      state,
    }),
  );

  const url = await authorizeUrl(config, verifier, window.location.origin, state);

  window.location.assign(url);
}

/** Complete the redirect: exchange the ?code= for tokens and store
    the session. Returns the email signed in with. */
export async function handleCallback(
  params: URLSearchParams,
): Promise<string | undefined> {
  // The authorization code and PKCE record are single-use. This
  // effect's dependencies can re-fire while the exchange is in
  // flight (document refresh changes the callback's dep identity),
  // and a second invocation consumed the PKCE record the first one
  // had already taken — failing the whole sign-in. Every call in a
  // tab shares one exchange; a full-page sign-in redirect resets it.
  callbackExchange ??= performCallback(params).catch(
    (reason: unknown) => {
      releaseExchangeSlot();
      throw reason;
    },
  );
  return callbackExchange;
}

let callbackExchange: Promise<string | undefined> | null = null;

// A failed exchange releases the slot so a genuine re-attempt (new
// code from a fresh Sign in click) is not stuck on the old promise.
function releaseExchangeSlot(): void {
  callbackExchange = null;
}

async function performCallback(
  params: URLSearchParams,
): Promise<string | undefined> {
  const config = await loadAuthConfig();

  if (!config) throw new Error("This backend does not require sign-in");

  const code = params.get("code");
  const state = params.get("state");

  if (!code) throw new Error("Sign-in redirect carried no authorization code");

  const stored = sessionStorage.getItem(PKCE_KEY);

  if (!stored) throw new Error("No pending sign-in found for this tab");

  const pkce = JSON.parse(stored) as {
    verifier: string;
    redirect_uri: string;
    state: string;
  };
  sessionStorage.removeItem(PKCE_KEY);

  if (state && pkce.state && state !== pkce.state) {
    throw new Error("Sign-in state mismatch (possible CSRF)");
  }

  const exchange = tokenExchangeRequest(
    config,
    code,
    pkce.redirect_uri,
    pkce.verifier,
  );
  const response = await fetch(exchange.url, exchange.init);

  if (!response.ok) throw new Error("Token exchange failed");

  const session = sessionFromTokenResponse(await response.json());

  writeSession(session);
  emit(session);

  return session.email;
}

/** A Bearer token for an API call — renewing the session when the
    id token expired. Rejects when there is nothing to renew. */
export async function getIdToken(): Promise<string | null> {
  const session = readSession();

  if (isSessionUsable(session)) return session!.id_token;

  return forceRefresh();
}

/** Forced renewal — used by the API client after a 401, when the
    stored token still LOOKS fresh (clock skew) but the authorizer
    already rejected it. Returns the new token or null. */
export async function forceRefresh(): Promise<string | null> {
  const session = readSession();

  if (!session?.refresh_token) {
    if (session) {
      // Present but expired/unusable, nothing to renew.
      writeSession(null);
      emit(null);
    }
    return null;
  }

  const config = await loadAuthConfig();

  if (!config) return null;

  const refresh = refreshRequest(config, session.refresh_token);
  const response = await fetch(refresh.url, refresh.init);

  if (!response.ok) {
    // A refresh reject means the session is dead - no retry loop.
    writeSession(null);
    emit(null);
    return null;
  }

  const next = sessionFromTokenResponse(await response.json());

  // A refresh grant does NOT rotate the refresh token for the
  // SPA flow, so carry the original one over if absent.
  if (!next.refresh_token) next.refresh_token = session.refresh_token;

  writeSession(next);
  emit(next);
  return next.id_token;
}

export function signOut(): void {
  writeSession(null);
  emit(null);

  const config = cachedConfig;

  if (config) {
    // Full-page redirect to Cognito's logout so the hosted-UI cookie
    // is cleared too; the logout_uri lands back on the app root.
    window.location.assign(logoutUrl(config, window.location.origin));
  }
}