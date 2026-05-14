import { describe, it, expect } from "vitest";
import {
  formatRelative,
  formatTimestamp,
  truncate,
  truncateHash,
} from "./format";

describe("formatRelative", () => {
  const now = new Date("2026-05-13T14:00:00Z");

  it("returns 'just now' for the same instant", () => {
    expect(formatRelative("2026-05-13T14:00:00Z", now)).toBe("just now");
  });

  it("formats past minutes", () => {
    expect(formatRelative("2026-05-13T13:57:00Z", now)).toBe("3m ago");
  });

  it("formats past hours", () => {
    expect(formatRelative("2026-05-13T12:00:00Z", now)).toBe("2h ago");
  });

  it("formats past days", () => {
    expect(formatRelative("2026-05-10T14:00:00Z", now)).toBe("3d ago");
  });

  it("formats future times with 'in' prefix", () => {
    expect(formatRelative("2026-05-13T14:05:00Z", now)).toBe("in 5m");
  });

  it("falls back to the input string for unparseable timestamps", () => {
    expect(formatRelative("not-a-date", now)).toBe("not-a-date");
  });
});

describe("formatTimestamp", () => {
  it("returns a non-empty formatted string for a valid ISO timestamp", () => {
    const out = formatTimestamp("2026-05-13T14:00:00Z");
    expect(out).not.toBe("");
    expect(out).not.toBe("2026-05-13T14:00:00Z");
  });

  it("falls back to the input string for unparseable timestamps", () => {
    expect(formatTimestamp("nonsense")).toBe("nonsense");
  });
});

describe("truncate", () => {
  it("leaves short strings alone", () => {
    expect(truncate("hello", 10)).toBe("hello");
  });

  it("truncates long strings with an ellipsis", () => {
    expect(truncate("hello world", 5)).toBe("hello…");
  });

  it("clamps non-positive limits to 1", () => {
    expect(truncate("hello", 0)).toBe("h…");
  });
});

describe("truncateHash", () => {
  it("returns the hash unchanged if shorter than head+tail", () => {
    expect(truncateHash("abc", 8, 4)).toBe("abc");
  });

  it("formats long hashes as head…tail", () => {
    expect(truncateHash("9f1c2a8b4f7e3d2a0001", 8, 4)).toBe("9f1c2a8b…0001");
  });
});
