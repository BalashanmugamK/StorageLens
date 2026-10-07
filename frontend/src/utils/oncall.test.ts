// Pure on-call scheduling rules — the OnCall page must resolve
// coverage with the backend's single definition (half-open windows,
// newest start wins per role), so the helpers are pinned here rather
// than left to the JSX.

import { describe, expect, it } from "vitest";
import {
  formatShiftRange,
  overlappingShiftIds,
  pdSeverityForState,
  resolveOnCall,
  shiftsCoveringNow,
  SYNC_STATUS_LABELS,
  syncStatusColor,
  toDatetimeLocalValue,
  validateShiftForm,
} from "./oncall";
import type { ApiOnCallShift } from "../types";

function shift(fields: Partial<ApiOnCallShift>): ApiOnCallShift {
  return {
    shift_id: "s1",
    engineer_email: "dana@example.com",
    engineer_name: "Dana",
    pagerduty_user_id: "PD123",
    role: "primary",
    start_at: "2026-03-01T09:00:00Z",
    end_at: "2026-03-01T17:00:00Z",
    created_by: "admin@example.com",
    created_at: "2026-02-20T09:00:00Z",
    updated_at: "2026-02-20T09:00:00Z",
    sync_status: "synced",
    ...fields,
  };
}

const NOW = "2026-03-01T12:00:00Z";

describe("shiftsCoveringNow", () => {
  it("uses half-open windows: start_at <= now < end_at", () => {
    const shifts = [
      shift({ shift_id: "s1", start_at: "2026-03-01T09:00:00Z", end_at: "2026-03-01T12:00:00Z" }),
      shift({ shift_id: "s2", start_at: "2026-03-01T12:00:00Z", end_at: "2026-03-01T20:00:00Z" }),
    ];
    expect(shiftsCoveringNow(shifts, NOW).map((s) => s.shift_id)).toEqual([
      "s2",
    ]);
  });

  it("excludes shifts that start later or already ended", () => {
    const shifts = [
      shift({ shift_id: "future", start_at: "2026-03-02T09:00:00Z", end_at: "2026-03-02T17:00:00Z" }),
      shift({ shift_id: "past", start_at: "2026-02-28T09:00:00Z", end_at: "2026-02-28T17:00:00Z" }),
    ];
    expect(shiftsCoveringNow(shifts, NOW)).toEqual([]);
  });

  it("drops rows with unparsable timestamps instead of crashing", () => {
    const shifts = [
      shift({ shift_id: "broken-start", start_at: "not-a-date" }),
      shift({ shift_id: "broken-end", end_at: "" }),
    ];
    expect(shiftsCoveringNow(shifts, NOW)).toEqual([]);
  });

  it("accepts a Date as now", () => {
    const shifts = [shift({ shift_id: "s1" })];
    expect(shiftsCoveringNow(shifts, new Date(NOW)).map((s) => s.shift_id)).toEqual([
      "s1",
    ]);
  });
});

describe("resolveOnCall", () => {
  it("returns one winner per role and covered for both", () => {
    const shifts = [
      shift({ shift_id: "p", role: "primary" }),
      shift({ shift_id: "sec", role: "secondary" }),
    ];
    const coverage = resolveOnCall(shifts, NOW);
    expect(coverage.primary?.shift_id).toBe("p");
    expect(coverage.secondary?.shift_id).toBe("sec");
    expect(coverage.coverage).toBe("covered");
    expect(coverage.overlapping).toEqual([]);
    expect(coverage.now).toBe("2026-03-01T12:00:00.000Z");
  });

  it("lets the newest start_at win per role and flags the displaced shift", () => {
    const shifts = [
      shift({ shift_id: "old-p", start_at: "2026-02-27T09:00:00Z", end_at: "2026-03-05T09:00:00Z" }),
      shift({ shift_id: "new-p", start_at: "2026-03-01T00:00:00Z", end_at: "2026-03-03T00:00:00Z" }),
    ];
    const coverage = resolveOnCall(shifts, NOW);
    expect(coverage.primary?.shift_id).toBe("new-p");
    expect(coverage.overlapping.map((s) => s.shift_id)).toEqual(["old-p"]);
  });

  it("resolves primary and secondary independently", () => {
    const shifts = [
      shift({ shift_id: "p", role: "primary" }),
      shift({ shift_id: "p2", role: "primary", start_at: "2026-03-01T10:00:00Z" }),
      shift({ shift_id: "sec", role: "secondary" }),
    ];
    const coverage = resolveOnCall(shifts, NOW);
    expect(coverage.primary?.shift_id).toBe("p2");
    expect(coverage.secondary?.shift_id).toBe("sec");
  });

  it("reports none and nulls when nobody covers now", () => {
    const shifts = [
      shift({ shift_id: "future", start_at: "2026-03-02T09:00:00Z", end_at: "2026-03-02T17:00:00Z" }),
    ];
    const coverage = resolveOnCall(shifts, NOW);
    expect(coverage.coverage).toBe("none");
    expect(coverage.primary).toBeNull();
    expect(coverage.secondary).toBeNull();
  });
});

describe("overlappingShiftIds", () => {
  it("flags same-role shifts whose windows intersect", () => {
    const shifts = [
      shift({ shift_id: "a", role: "primary", start_at: "2026-03-01T09:00:00Z", end_at: "2026-03-01T17:00:00Z" }),
      shift({ shift_id: "b", role: "primary", start_at: "2026-03-01T15:00:00Z", end_at: "2026-03-01T23:00:00Z" }),
      shift({ shift_id: "sec", role: "secondary", start_at: "2026-03-01T09:00:00Z", end_at: "2026-03-01T23:00:00Z" }),
    ];
    expect(overlappingShiftIds(shifts)).toEqual(new Set(["a", "b"]));
  });

  it("does not flag touching shifts (half-open windows)", () => {
    const shifts = [
      shift({ shift_id: "a", start_at: "2026-03-01T09:00:00Z", end_at: "2026-03-01T17:00:00Z" }),
      shift({ shift_id: "b", start_at: "2026-03-01T17:00:00Z", end_at: "2026-03-02T01:00:00Z" }),
    ];
    expect(overlappingShiftIds(shifts)).toEqual(new Set());
  });
});

describe("pdSeverityForState", () => {
  it("maps ALARM to critical", () => {
    expect(pdSeverityForState("ALARM")).toBe("critical");
  });

  it("maps OK to info and INSUFFICIENT_DATA to warning", () => {
    expect(pdSeverityForState("OK")).toBe("info");
    expect(pdSeverityForState("INSUFFICIENT_DATA")).toBe("warning");
  });

  it("defaults unknown or missing states to error", () => {
    expect(pdSeverityForState("")).toBe("error");
    expect(pdSeverityForState(undefined)).toBe("error");
  });
});

describe("SYNC_STATUS_LABELS / syncStatusColor", () => {
  it("covers every sync status the backend stamps", () => {
    for (const status of [
      "pending",
      "synced",
      "skipped_missing_pd_user_id",
      "skipped_no_secret",
      "skipped_no_schedule",
      "failed",
      "removed",
    ] as const) {
      expect(SYNC_STATUS_LABELS[status], status).toBeTruthy();
      expect(SYNC_STATUS_LABELS[status].label.length).toBeGreaterThan(0);
      expect(syncStatusColor(status)).toMatch(/^var\(--/);
    }
    expect(Object.keys(SYNC_STATUS_LABELS)).toHaveLength(7);
  });
});

describe("formatShiftRange", () => {
  // Naive (no-offset) strings parse AND render in the host timezone,
  // so these assertions hold under any TZ the tests run in.
  it("spells the date once for a same-day shift", () => {
    const text = formatShiftRange(
      "2026-03-01T09:00:00",
      "2026-03-01T17:00:00",
    );
    expect(text).toContain("Mar 1, 2026");
    expect(text).toContain("–");
  });

  it("spells both ends for a shift crossing midnight", () => {
    const text = formatShiftRange(
      "2026-03-01T09:00:00",
      "2026-03-04T17:00:00",
    );
    expect(text).toContain("Mar 1, 2026");
    expect(text).toContain("Mar 4, 2026");
    expect(text).toContain("→");
  });

  it("falls back to an em dash for missing or unparsable bounds", () => {
    expect(formatShiftRange(null, "2026-03-01T17:00:00Z")).toBe("—");
    expect(formatShiftRange("2026-03-01T09:00:00Z", "")).toBe("—");
    expect(formatShiftRange("nonsense", "nonsense")).toBe("—");
  });
});

describe("toDatetimeLocalValue", () => {
  it("round-trips the instant through the local wall clock", () => {
    const iso = "2026-03-01T12:00:00Z";
    const value = toDatetimeLocalValue(iso);
    expect(value).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    // Both parses resolve in the host timezone, so the instants must agree.
    expect(new Date(value).getTime()).toBe(new Date(iso).getTime());
  });

  it("returns an empty string for missing or unparsable input", () => {
    expect(toDatetimeLocalValue(null)).toBe("");
    expect(toDatetimeLocalValue(undefined)).toBe("");
    expect(toDatetimeLocalValue("nonsense")).toBe("");
  });
});

describe("validateShiftForm", () => {
  const valid = {
    engineer_email: "dana@example.com",
    engineer_name: "  Dana Diaz  ",
    pagerduty_user_id: " PD123 ",
    role: "primary",
    start_at: "2026-03-01T09:00:00",
    end_at: "2026-03-01T17:00:00",
  };

  it("accepts a well-formed shift and trims/normalizes the input", () => {
    const { errors, cleaned } = validateShiftForm(valid);
    expect(errors).toEqual([]);
    expect(cleaned).not.toBeNull();
    expect(cleaned?.engineer_email).toBe("dana@example.com");
    expect(cleaned?.engineer_name).toBe("Dana Diaz");
    expect(cleaned?.pagerduty_user_id).toBe("PD123");
    // datetime-local input is read in the viewer's local time and
    // normalized to UTC ISO-8601 for the backend.
    expect(cleaned?.start_at.endsWith("Z")).toBe(true);
    expect(cleaned?.end_at.endsWith("Z")).toBe(true);
  });

  it("leaves optional fields undefined when blank", () => {
    const { cleaned } = validateShiftForm({
      ...valid,
      engineer_name: "   ",
      pagerduty_user_id: "",
    });
    expect(cleaned?.engineer_name).toBeUndefined();
    expect(cleaned?.pagerduty_user_id).toBeUndefined();
  });

  it("rejects a blank or malformed email", () => {
    for (const email of ["  ", "dana", "dana@example", "da na@example.com"]) {
      const { errors, cleaned } = validateShiftForm({
        ...valid,
        engineer_email: email,
      });
      expect(errors, email).toContainEqual(
        expect.stringContaining("engineer_email"),
      );
      expect(cleaned).toBeNull();
    }
  });

  it("rejects a role outside primary/secondary", () => {
    const { errors, cleaned } = validateShiftForm({ ...valid, role: "tertiary" });
    expect(errors).toContainEqual(expect.stringContaining("role must be one of"));
    expect(cleaned).toBeNull();
  });

  it("rejects unparsable datetimes", () => {
    const { errors, cleaned } = validateShiftForm({
      ...valid,
      start_at: "yesterday-ish",
    });
    expect(errors).toContainEqual(expect.stringContaining("start_at must be an ISO-8601"));
    expect(cleaned).toBeNull();
  });

  it("rejects a start after the end (and an exact tie)", () => {
    const { errors, cleaned } = validateShiftForm({
      ...valid,
      start_at: "2026-03-01T17:00:00",
      end_at: "2026-03-01T17:00:00",
    });
    expect(errors).toContainEqual(expect.stringContaining("start_at must be before end_at"));
    expect(cleaned).toBeNull();
  });

  it("rejects shifts longer than 7 days", () => {
    const { errors, cleaned } = validateShiftForm({
      ...valid,
      start_at: "2026-03-01T09:00:00",
      end_at: "2026-03-09T09:00:01",
    });
    expect(errors).toContainEqual(expect.stringContaining("at most 7 days"));
    expect(cleaned).toBeNull();
  });

  it("accepts a shift of exactly 7 days", () => {
    const { errors, cleaned } = validateShiftForm({
      ...valid,
      start_at: "2026-03-01T09:00:00",
      end_at: "2026-03-08T09:00:00",
    });
    expect(errors).toEqual([]);
    expect(cleaned).not.toBeNull();
  });
});