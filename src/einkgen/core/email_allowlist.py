"""Sender allowlist for the inbound-email Lambda.

The allowlist is a plain-text file at ``s3://<bucket>/config/email_allowlist.txt``:

    # one email per line; blank lines and #-prefixed lines are ignored
    me@example.com
    spouse@example.com

It's a file (not a Secrets Manager secret) so an operator can edit it
straight from the AWS console, the CLI, or ``aws s3 cp`` — there's no PII
worth Secrets-Manager-ing here, and using S3 keeps the Lambda's secret-fetch
surface to one (OpenAI). The Lambda also keeps the loaded list in module
scope to amortise the GetObject across warm invocations.
"""

from __future__ import annotations

import time

from einkgen.core import s3

ALLOWLIST_KEY = "config/email_allowlist.txt"

# Bound how long a removed sender stays accepted on warm Lambda containers.
# Mirrors the device_status token TTL.
ALLOWLIST_CACHE_TTL_SECONDS = 60

_cached: frozenset[str] | None = None
_cached_at: float = 0.0


def _reset_cache() -> None:
    """Drop the cached allowlist. Used by tests."""
    global _cached, _cached_at
    _cached = None
    _cached_at = 0.0


def _normalize(email: str) -> str:
    """Lowercase + trim. Email addresses are case-insensitive in practice."""
    return email.strip().lower()


def _parse(body: str) -> frozenset[str]:
    entries: set[str] = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate trailing comments on a line: `me@example.com  # mine`
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
        entries.add(_normalize(line))
    return frozenset(entries)


def load(*, force: bool = False) -> frozenset[str]:
    """Return the current allowlist, refreshing from S3 if stale.

    A missing allowlist file is treated as an empty allowlist — the inbound
    Lambda will then reject every sender, which is the safe default for a
    first-deploy state where the operator hasn't populated the file yet.
    """
    global _cached, _cached_at
    now = time.monotonic()
    if not force and _cached is not None and (now - _cached_at) < ALLOWLIST_CACHE_TTL_SECONDS:
        return _cached
    head = s3.head_object(ALLOWLIST_KEY)
    if head is None:
        _cached = frozenset()
    else:
        body = s3.get_object(ALLOWLIST_KEY).decode("utf-8", errors="replace")
        _cached = _parse(body)
    _cached_at = now
    return _cached


def is_allowed(email: str | None) -> bool:
    """Return True iff `email` is on the allowlist (case-insensitive)."""
    if not email:
        return False
    return _normalize(email) in load()


def write(emails: list[str]) -> None:
    """Replace the allowlist with the given emails (used by the CLI).

    Entries are deduplicated, normalised, sorted, and written one per line
    with a leading comment header. Comments in the existing file are not
    preserved — if you need rich annotations, edit the file directly.
    """
    cleaned = sorted({_normalize(e) for e in emails if e and e.strip()})
    header = (
        "# einkgen email allowlist — senders permitted to enqueue via inbound email.\n"
        "# One address per line. Lines starting with # are ignored.\n"
        "# Managed by `einkgen allowlist {ls,add,rm}` but free to edit by hand.\n"
    )
    body = header + "\n".join(cleaned) + ("\n" if cleaned else "")
    s3.put_object(ALLOWLIST_KEY, body.encode("utf-8"), content_type="text/plain; charset=utf-8")
    _reset_cache()
