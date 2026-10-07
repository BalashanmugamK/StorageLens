// Sign-in chip in the top bar. Hidden entirely while auth
// requirements are unknown, and in builds without a pool — pages
// then rely on the API client surfacing 401s as errors, which the
// caller renders as a sign-in requirement.

import { LogIn, LogOut } from "lucide-react";
import { useAuthStatus } from "../../hooks/useAuth";
import { signIn, signOut } from "../../services/auth";

export function AuthChip() {
  const { required, session } = useAuthStatus();

  if (required !== true) return null;

  if (session?.email) {
    return (
      <span
        className="meta-chip hide-mobile"
        title="Signed in through Cognito (JWT authorizer on every API route)"
      >
        {session.email}
        <button
          type="button"
          className="chip-action"
          onClick={() => signOut()}
          title="Sign out (clears this tab's token session and the hosted-UI cookie)"
          aria-label="Sign out"
          style={{ display: "inline-flex", alignItems: "center", marginLeft: 6, background: "none", border: "none", cursor: "pointer", color: "inherit", padding: 0 }}
        >
          <LogOut size={12} />
        </button>
      </span>
    );
  }

  return (
    <span className="meta-chip hide-mobile">
      <button
        type="button"
        className="chip-action"
        onClick={() => void signIn()}
        title="Sign in through the Cognito hosted UI"
        style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "none", border: "none", cursor: "pointer", color: "inherit", padding: 0, font: "inherit" }}
      >
        <LogIn size={12} /> Sign in
      </button>
    </span>
  );
}