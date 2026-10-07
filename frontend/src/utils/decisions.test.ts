// Pure decision-lifecycle rules — the Approvals page must only ever
// offer actions the backend actually accepts, so the mapping is pinned
// here rather than left to the JSX.

import { describe, expect, it } from "vitest";
import {
  actionsForState,
  isInternalDecisionRow,
  queueForState,
  STATE_LABELS,
} from "./decisions";
import type { DecisionState } from "../types";

const ALL_STATES: DecisionState[] = [
  "proposed",
  "approved",
  "rejected",
  "verified",
  "failed",
];

describe("actionsForState", () => {
  it("offers approve + reject only on proposed", () => {
    expect(actionsForState("proposed")).toEqual(["approve", "reject"]);
  });

  it("offers execute only on approved", () => {
    expect(actionsForState("approved")).toEqual(["execute"]);
  });

  it("offers no actions on terminal states", () => {
    for (const state of ["rejected", "verified", "failed"] as const) {
      expect(actionsForState(state)).toEqual([]);
    }
  });
});

describe("queueForState", () => {
  it("routes proposed and approved to actionable tabs", () => {
    expect(queueForState("proposed")).toBe("queue");
    expect(queueForState("approved")).toBe("approved");
  });

  it("routes every terminal state to history", () => {
    for (const state of ["rejected", "verified", "failed"] as const) {
      expect(queueForState(state)).toBe("history");
    }
  });
});

describe("isInternalDecisionRow", () => {
  it("flags the open-decision guard rows by key and state", () => {
    expect(
      isInternalDecisionRow({
        decision_id: "OPENDEC#doc-9",
        decision_state: "__open_marker__",
      }),
    ).toBe(true);
    expect(
      isInternalDecisionRow({
        decision_id: "d-1",
        decision_state: "__open_marker__" as DecisionState,
      }),
    ).toBe(true);
    expect(
      isInternalDecisionRow({
        decision_id: "OPENDEC#doc-9",
        decision_state: "proposed",
      }),
    ).toBe(true);
  });

  it("passes every real decision state through", () => {
    for (const state of ALL_STATES) {
      expect(
        isInternalDecisionRow({ decision_id: "d-1", decision_state: state }),
      ).toBe(false);
    }
  });
});

describe("STATE_LABELS", () => {
  it("covers every decision state the backend emits", () => {
    for (const state of ALL_STATES) {
      expect(STATE_LABELS[state], state).toBeTruthy();
      expect(STATE_LABELS[state].label.length).toBeGreaterThan(0);
      expect(STATE_LABELS[state].meaning.length).toBeGreaterThan(0);
    }
    expect(Object.keys(STATE_LABELS)).toHaveLength(ALL_STATES.length);
  });
});