// Tests for the PKCE/URL/token logic in services/authLogic.ts — the
// pure layer of the sign-in flow. These derive expected values by
// hand (round-trips, URL query shapes) rather than re-calling the
// functions under test where that would just mirror the code.

import { describe, expect, it } from "vitest";
import {
  type Session,
  authorizeUrl,
  base64url,
  codeChallenge,
  createCodeVerifier,
  decodeIdTokenClaims,
  isSessionUsable,
  logoutUrl,
  refreshRequest,
  sessionFromTokenResponse,
  tokenExchangeRequest,
} from "./authLogic";

const CONFIG = {
  user_pool_id: "eu-west-3_TEST",
  client_id: "client-1",
  auth_domain: "my-stack-auth",
};

describe("PKCE verifier + challenge", () => {
  it("verifiers are URL-safe, long, and fresh each call", () => {
    const a = createCodeVerifier();
    const b = createCodeVerifier();

    expect(a).not.toBe(b);
    expect(a).toMatch(/^[A-Za-z0-9_-]+$/);
    // 64 random bytes -> 86 base64url chars (no padding).
    expect(a.length).toBe(86);
    expect(a.length).toBeGreaterThan(43); // RFC 7636 floor
  });

  it("base64url matches hand-shaped vectors for every special case", () => {
    expect(base64url(new Uint8Array([0xf, 0xbf, 0xfe])))
      .toBe("D7_-"); // '+'->'-', '/'->'_', no '='

    expect(base64url(new Uint8Array([0x00]))).toBe("AA");
    // [6]b11111111 -> 2 bits '11' padded: 'AB E=' hand-encoded
    expect(base64url(new Uint8Array([0xff]))).toBe("_w");
  });

  it("S256 challenge of a known verifier matches the RFC vector", async () => {
    // RFC 7636 appendix B uses plain base64url without padding.
    const challenge = await codeChallenge(
      "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    );
    expect(challenge).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });
});

describe("hosted-UI URLs", () => {
  it("authorize URL carries the full code+PKCE query", async () => {
    const url = await authorizeUrl(
      CONFIG,
      "verifier",
      "http://localhost:5173",
      "state-1",
    );

    expect(url.startsWith("https://my-stack-auth/oauth2/authorize?")).toBe(true);

    const params = new URL(new URL(url).toString()).searchParams;

    expect(params.get("response_type")).toBe("code");
    expect(params.get("client_id")).toBe("client-1");
    expect(params.get("redirect_uri")).toBe(
      "http://localhost:5173/auth/callback",
    );
    expect(params.get("scope")).toBe("openid email");
    expect(params.get("code_challenge_method")).toBe("S256");
    expect(params.get("state")).toBe("state-1");
    // challenge is 256 bits base64url
    expect(params.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("token exchange posts the code grant with the verifier", () => {
    const { url, init } = tokenExchangeRequest(
      CONFIG,
      "auth-code",
      "http://localhost:5173",
      "v",
    );

    expect(url).toBe("https://my-stack-auth/oauth2/token");
    expect(init.method).toBe("POST");

    const params = new URLSearchParams(init.body as string);

    expect(params.get("grant_type")).toBe("authorization_code");
    expect(params.get("client_id")).toBe("client-1");
    expect(params.get("code")).toBe("auth-code");
    expect(params.get("code_verifier")).toBe("v");
    expect(params.get("redirect_uri")).toBe(
      "http://localhost:5173/auth/callback",
    );
  });

  it("refresh grant posts only the refresh token", () => {
    const { init } = refreshRequest(CONFIG, "rt-1");

    const params = new URLSearchParams(init.body as string);

    expect(params.get("grant_type")).toBe("refresh_token");
    expect(params.get("refresh_token")).toBe("rt-1");
    expect(params.has("code_verifier")).toBe(false);
  });

  it("logout URL returns the caller to the app root", () => {
    const url = logoutUrl(CONFIG, "http://localhost:5173");

    expect(url.startsWith("https://my-stack-auth/logout?")).toBe(true);

    const params = new URL(url).searchParams;

    expect(params.get("client_id")).toBe("client-1");
    expect(params.get("logout_uri")).toBe("http://localhost:5173/");
  });
});

// Hand-built JWT: header {"alg":"RS256","typ":"JWT"} claims
// {"email":"a@b.c","exp":1700000060} — the exact base64url of each
// segment was computed independently and pasted here.
const ID_TOKEN =
  "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." +
  "eyJlbWFpbCI6ImFAYi5jIiwiZXhwIjoxNzAwMDAwMDYwfQ." +
  "sig";

describe("token session", () => {
  it("decodes id-token claims from a hand-built JWT", () => {
    const claims = decodeIdTokenClaims(ID_TOKEN);

    expect(claims.email).toBe("a@b.c");
    expect(claims.exp).toBe(1700000060);
  });

  it("rejects malformed tokens", () => {
    expect(() => decodeIdTokenClaims("no-dots")).toThrow();
    expect(() =>
      decodeIdTokenClaims("h.!!!notbase64url!!!.s"),
    ).toThrow();
  });

  it("builds a session from a token response", () => {
    const session: Session = sessionFromTokenResponse(
      {
        id_token: ID_TOKEN,
        refresh_token: "rt",
        expires_in: 3600,
      },
    );

    expect(session.id_token).toBe(ID_TOKEN);
    expect(session.refresh_token).toBe("rt");
    expect(session.email).toBe("a@b.c");
    // expires_at comes from the JWT's exp, NOT from expires_in.
    expect(session.expires_at).toBe(1700000060000);
  });

  it("rejects a token response without a usable exp claim", () => {
    const noExp =
      "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
      + "eyJlbWFpbCI6ImFAYi5jIn0."
      + "sig";

    expect(() =>
      sessionFromTokenResponse({ id_token: noExp, expires_in: 1 }),
    ).toThrow(/exp/);
  });

  it("usability reserves 30 s of skew", () => {
    const session: Session = {
      id_token: "t",
      expires_at: 2000000,
    };

    // usable iff expires_at > now + 30_000
    expect(isSessionUsable(session, 2000000)).toBe(false);
    expect(isSessionUsable(session, 1970001)).toBe(false);
    // exact boundary: now + 30_000 == expires_at is NOT usable
    expect(isSessionUsable(session, 1970000)).toBe(false);
    expect(isSessionUsable(session, 1969999)).toBe(true);
    expect(isSessionUsable(null, 0)).toBe(false);
  });
});