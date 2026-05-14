// Pure formatting helpers. Kept dependency-free so they're trivial to unit test.

const MIN_S = 60;
const HOUR_S = 60 * MIN_S;
const DAY_S = 24 * HOUR_S;

/**
 * Format an ISO timestamp as an absolute, locale-readable string.
 * Returns the original input if it can't be parsed — callers should treat
 * formatted output as display-only.
 */
export function formatTimestamp(iso: string, now: Date = new Date()): string {
  void now;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Format an ISO timestamp as a short relative string ("3m ago", "in 2h").
 * Falls back to the original string if it can't be parsed.
 */
export function formatRelative(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffSec = Math.round((d.getTime() - now.getTime()) / 1000);
  const past = diffSec < 0;
  const abs = Math.abs(diffSec);

  let value: string;
  if (abs < 5) {
    value = "just now";
    return value;
  } else if (abs < MIN_S) {
    value = `${abs}s`;
  } else if (abs < HOUR_S) {
    value = `${Math.floor(abs / MIN_S)}m`;
  } else if (abs < DAY_S) {
    value = `${Math.floor(abs / HOUR_S)}h`;
  } else {
    value = `${Math.floor(abs / DAY_S)}d`;
  }
  return past ? `${value} ago` : `in ${value}`;
}

/**
 * Truncate a string to `n` chars, appending an ellipsis if shortened.
 * `n` must be at least 1; values below that are clamped.
 */
export function truncate(s: string, n: number): string {
  const limit = Math.max(1, n);
  if (s.length <= limit) return s;
  return `${s.slice(0, limit)}…`;
}

/**
 * Truncate a hex-like hash to a head…tail preview ("9f1c…a32b").
 * If the input is shorter than `head + tail`, returns it unchanged.
 */
export function truncateHash(hash: string, head = 8, tail = 4): string {
  if (hash.length <= head + tail) return hash;
  return `${hash.slice(0, head)}…${hash.slice(-tail)}`;
}
