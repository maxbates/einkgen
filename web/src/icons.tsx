// Inline SVG icons for queue actions. Vibe matches Apple Music's
// "Play Next" / "Play Last" / "Play Now" controls — small, monochrome,
// `currentColor` strokes so they pick up the surrounding button color.

const COMMON = {
  width: 16,
  height: 16,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

/** Add-to-top: a play head pinned above three queue lines. */
export function IconAddToTop() {
  return (
    <svg {...COMMON}>
      <path d="M4 3l2 -2 2 2" />
      <path d="M6 1v5" />
      <path d="M3 8h10" />
      <path d="M3 11h10" />
      <path d="M3 14h10" />
    </svg>
  );
}

/** Add-to-bottom: three queue lines with a play head dropped below. */
export function IconAddToBottom() {
  return (
    <svg {...COMMON}>
      <path d="M3 2h10" />
      <path d="M3 5h10" />
      <path d="M3 8h10" />
      <path d="M6 10v5" />
      <path d="M4 13l2 2 2 -2" />
    </svg>
  );
}

/** Play-now: a play triangle, same as every UI ever. */
export function IconPlayNow() {
  return (
    <svg {...COMMON} fill="currentColor" stroke="none">
      <path d="M4 3l9 5l-9 5z" />
    </svg>
  );
}

/** Trash / remove. */
export function IconRemove() {
  return (
    <svg {...COMMON}>
      <path d="M3 4h10" />
      <path d="M6 4V2.5a.5 .5 0 0 1 .5 -.5h3a.5 .5 0 0 1 .5 .5V4" />
      <path d="M4.5 4l.7 9.1a1 1 0 0 0 1 .9h3.6a1 1 0 0 0 1 -.9L11.5 4" />
    </svg>
  );
}
