// Pure logic for the Cognito hosted-UI authorization-code + PKCE
// sign-in flow — NO browser APIs (storage, location, navigator):
// every effect-producing call lives in auth.ts, which wires these
// helpers to sessionStorage/window.location. This split keeps the
// crypto and URL-shape logic unit-testable without a DOM.

export interface AuthConfig {
  user_pool_id: string;
  client_id: string;
  auth_domain: string;
}

export interface Session {
  id_token: string;
  refresh_token?: string;
  /** epoch ms after which the id token needs renewing */
  expires_at: number;
  /** signed-in email, decoded from the id token */
  email?: string;
}

// 64 entropy bytes is the RFC 7636 minimum-strength recommendation,
// well above Cognito's verifier length floor.
const VERIFIER_BYTES = 64;

export function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64url(text: string): string {
  const base = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base + "=".repeat((4 - (base.length % 4)) % 4);
  return atob(padded);
}

export function createCodeVerifier(): string {
  const bytes = new Uint8Array(VERIFIER_BYTES);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

export async function codeChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  return base64url(new Uint8Array(digest));
}

/** The GET the browser must make against the hosted-UI domain. */
export async function authorizeUrl(
  config: AuthConfig,
  verifier: string,
  redirectUri: string,
  state: string,
): Promise<string> {
  const params = new URLSearchParams({
    response_type: "code",
    client_id: config.client_id,
    redirect_uri: `${redirectUri}/auth/callback`,
    scope: "openid email",
    code_challenge: await codeChallenge(verifier),
    code_challenge_method: "S256",
    state,
  });

  return `https://${config.auth_domain}/oauth2/authorize?${params}`;
}

export interface TokenResponse {
  id_token: string;
  refresh_token?: string;
  expires_in: number;
}

function tokenRequestBody(params: Record<string, string>): string {
  return new URLSearchParams(params).toString();
}

/** The POST that swaps an authorization code for tokens. No client
    secret: the SPA client is public and PKCE carries the binding. */
export function tokenExchangeRequest(
  config: AuthConfig,
  code: string,
  redirectUri: string,
  verifier: string,
): { url: string; init: RequestInit } {
  return {
    url: `https://${config.auth_domain}/oauth2/token`,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: tokenRequestBody({
        grant_type: "authorization_code",
        client_id: config.client_id,
        code,
        redirect_uri: `${redirectUri}/auth/callback`,
        code_verifier: verifier,
      }),
    },
  };
}

export function refreshRequest(
  config: AuthConfig,
  refreshToken: string,
): { url: string; init: RequestInit } {
  return {
    url: `https://${config.auth_domain}/oauth2/token`,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: tokenRequestBody({
        grant_type: "refresh_token",
        client_id: config.client_id,
        refresh_token: refreshToken,
      }),
    },
  };
}

/** The hosted-UI logout: clears the Cognito session cookie so the
    next sign-in is a genuine one. The endpoint is the classic
    /logout on the pool domain — this pool's authorization server
    rejects the newer /oauth2/logout with "This URL doesn't exist on
    the authorization server". */
export function logoutUrl(config: AuthConfig, redirectUri: string): string {
  const params = new URLSearchParams({
    client_id: config.client_id,
    logout_uri: `${redirectUri}/`,
  });

  return `https://${config.auth_domain}/logout?${params}`;
}

// Only claims the client needs; anything else passes through unused.
export interface IdTokenClaims {
  email?: string;
  exp?: number;
  [key: string]: unknown;
}

export function decodeIdTokenClaims(idToken: string): IdTokenClaims {
  const payload = idToken.split(".")[1];

  if (!payload) throw new Error("Malformed JWT: no payload segment");

  try {
    return JSON.parse(fromBase64url(payload)) as IdTokenClaims;
  } catch {
    throw new Error("Malformed JWT: payload is not valid JSON");
  }
}

export function sessionFromTokenResponse(tokens: TokenResponse): Session {
  if (!tokens.id_token) throw new Error("Token response carried no id_token");

  const claims = decodeIdTokenClaims(tokens.id_token);

  if (typeof claims.exp !== "number") {
    throw new Error("id_token carries no exp claim");
  }

  const session: Session = {
    id_token: tokens.id_token,
    // expires_at from the JWT's own exp, not from expires_in: the
    // issuer's clock is the one the authorizer honors.
    expires_at: claims.exp * 1000,
  };

  if (tokens.refresh_token) session.refresh_token = tokens.refresh_token;
  if (typeof claims.email === "string") session.email = claims.email;

  return session;
}

/** True when the session can still drive an API call. A 30 s skew
    reserve keeps a token from being handed over already-expired. */
export function isSessionUsable(
  session: Session | null,
  nowMs = Date.now(),
): boolean {
  return session !== null && session.expires_at > nowMs + 30_000;
}