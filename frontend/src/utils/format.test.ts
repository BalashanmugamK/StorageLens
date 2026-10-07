// Formatting helpers — the contract everything else in the UI rests on.

import { describe, expect, it } from "vitest";
import {
  fileExtension,
  formatBytes,
  formatGb,
  formatMoney,
  formatPercent,
  tierLabel,
} from "./format";

describe("formatMoney", () => {
  it("shows four decimals below one cent", () => {
    expect(formatMoney(0.000123)).toBe("$0.0001");
  });

  it("shows two decimals below one dollar", () => {
    expect(formatMoney(0.42)).toBe("$0.42");
  });

  it("rounds large values to whole dollars", () => {
    expect(formatMoney(923.4702)).toBe("$923");
  });

  it("returns $0 without decimals", () => {
    expect(formatMoney(0)).toBe("$0");
  });
});

describe("formatPercent", () => {
  it("keeps one decimal by default", () => {
    expect(formatPercent(82.9746)).toBe("83.0%");
  });

  it("respects an explicit precision", () => {
    expect(formatPercent(2.2534, 2)).toBe("2.25%");
  });
});

describe("formatBytes / formatGb", () => {
  it("scales bytes up the unit ladder", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024 * 1024 * 3)).toBe("3.0 MB");
  });

  it("formats gigabytes with one decimal", () => {
    expect(formatGb(127.412)).toBe("127.4 GB");
    expect(formatGb(1536)).toBe("1.5 TB");
  });
});

describe("tierLabel", () => {
  it("maps storage classes to display labels", () => {
    expect(tierLabel("GLACIER_DEEP_ARCHIVE")).toBe("Glacier Deep Archive");
    expect(tierLabel("STANDARD")).toBe("S3 Standard");
  });

  it("handles null and unknown classes", () => {
    expect(tierLabel(null)).toBe("—");
    expect(tierLabel("SOMETHING_ELSE")).toBe("SOMETHING_ELSE");
  });
});

describe("fileExtension", () => {
  it("extracts and uppercases the extension", () => {
    expect(fileExtension("opinion.pdf")).toBe("PDF");
    expect(fileExtension("no-extension")).toBe("FILE");
  });
});