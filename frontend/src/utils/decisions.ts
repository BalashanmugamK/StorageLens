// Decision lifecycle helpers — the pure layer behind the Approvals
// page, kept here so the lifecycle rules are unit-testable without a
// render: which actions a state allows, what each state is called and
// how it is colored. The backend owns the actual transitions; this
// only mirrors them so the UI never shows an impossible action.

import type { DecisionState } from "../types";

/** Friendly name (and one-line meaning) for every decision state. */
export const STATE_LABELS: Record<
  DecisionState,
  { label: string; meaning: string }
> = {
  proposed: {
    label: "Proposed",
    meaning: "Waiting for a reviewer to approve or reject",
  },
  approved: {
    label: "Approved",
    meaning: "Approved — execute to move the object in S3",
  },
  rejected: {
    label: "Rejected",
    meaning: "A reviewer rejected this migration",
  },
  verified: {
    label: "Verified",
    meaning: "S3 HEAD confirmed the new class; savings realized",
  },
  failed: {
    label: "Failed",
    meaning: "The transition or its verification failed",
  },
};

/** Lifecycle actions a state allows (order = button order in the UI). */
export function actionsForState(
  state: DecisionState,
): ("approve" | "reject" | "execute")[] {
  switch (state) {
    case "proposed":
      return ["approve", "reject"];
    case "approved":
      return ["execute"];
    default:
      return [];
  }
}

/** CSS color token for a state badge. */
export function stateColor(state: DecisionState): string {
  switch (state) {
    case "proposed":
      return "var(--accent-strong)";
    case "approved":
      return "var(--warning)";
    case "verified":
      return "var(--good)";
    case "failed":
      return "var(--danger)";
    case "rejected":
      return "var(--text-3)";
  }
}

/** The one-open-decision-per-document guard rows ride the decisions
    table as bookkeeping items (OPENDEC# keys, "__open_marker__"
    states). They carry no decision fields, so rendering them in a
    queue must never happen - the queues skip them defensively even
    though the backend filters them from listing responses. */
export function isInternalDecisionRow(decision: {
  decision_id: string;
  decision_state: string;
}): boolean {
  return (
    decision.decision_id.startsWith("OPENDEC#") ||
    decision.decision_state === "__open_marker__"
  );
}

/** Which queue tab a state belongs to. Verified/rejected/failed share
    the History view; proposed and approved are the actionable ones. */
export function queueForState(
  state: DecisionState,
): "queue" | "approved" | "history" {
  switch (state) {
    case "proposed":
      return "queue";
    case "approved":
      return "approved";
    default:
      return "history";
  }
}