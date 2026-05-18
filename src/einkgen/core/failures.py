"""Short-lived operator-visible record of permanently-dropped queue items.

When the generator Lambda drops an item via ``PermanentItemError`` (e.g.
OpenAI's safety system rejected the prompt with ``moderation_blocked``),
it writes a tiny JSON breadcrumb here so the operator sees *why* their
submission never appeared on the device. The Admin tab in the SPA polls
``GET /admin/failures`` and surfaces a "Recently rejected" panel.

These are notification artefacts, not durable records — the read path
filters out anything older than ``MAX_AGE_SECONDS`` (default: 1 hour),
and ``record()`` sweeps expired entries on write so the prefix stays
small. Failure to write a breadcrumb must never block the queue drain;
every S3 op here is best-effort.

Keys live at ``queue/failed/<recorded_at>-<id>.json``. The lex-sorted
timestamp prefix makes "newest first" a sort+reverse with no metadata
fetches. The path sits under ``queue/`` so it shares the bucket's
existing lifecycle/IAM, but ``einkgen.core.queue`` explicitly filters
this prefix out of the pending-item listing so it never appears on the
public Queue tab.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from einkgen.core import s3
from einkgen.core.queue import QueueItem

log = logging.getLogger(__name__)

FAILURE_PREFIX = "queue/failed/"

# How long a rejection breadcrumb is visible to the operator. After this
# they're hidden on read and best-effort deleted on the next write.
MAX_AGE_SECONDS = 3600

# Hard caps to keep records tiny regardless of upstream input. Prompt and
# reason are user-shaped strings; trim aggressively rather than carry raw
# 10KB error bodies into S3.
MAX_PROMPT_CHARS = 1000
MAX_REASON_CHARS = 500

# Defence in depth: never return more than this many records, even if the
# bucket somehow has more under the prefix.
MAX_RECORDS_RETURNED = 20


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime | None:
    """Parse our ISO format. Returns None on malformed input."""
    try:
        # Strict Z-suffixed format — we control both sides.
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _key_timestamp(ts: str) -> str:
    return ts.replace(":", "-")


def _truncate(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"  # ellipsis


@dataclass
class FailureRecord:
    id: str
    enqueued_at: str
    recorded_at: str
    source: str
    kind: str
    reason: str
    prompt: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "enqueued_at": self.enqueued_at,
            "recorded_at": self.recorded_at,
            "source": self.source,
            "kind": self.kind,
            "reason": self.reason,
            "prompt": self.prompt,
        }


def record(item: QueueItem, reason: str) -> None:
    """Persist a breadcrumb for a dropped item and sweep expired ones.

    Best-effort: any S3 error is logged and swallowed so the caller's
    queue drain isn't pinned by a notification side-channel failure.
    """
    recorded_at = _iso_now()
    rec = FailureRecord(
        id=item.id,
        enqueued_at=item.enqueued_at,
        recorded_at=recorded_at,
        source=item.source,
        kind=item.kind,
        reason=_truncate(reason, MAX_REASON_CHARS) or "",
        prompt=_truncate(item.prompt, MAX_PROMPT_CHARS),
    )
    key = f"{FAILURE_PREFIX}{_key_timestamp(recorded_at)}-{item.id}.json"
    try:
        s3.put_object(
            key,
            json.dumps(rec.to_json()).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        log.exception("failed to write failure breadcrumb for item %s", item.id)
        return
    _sweep_expired()


def list_recent() -> list[FailureRecord]:
    """Return surviving breadcrumbs (newest first, capped).

    Read-side filtering by ``recorded_at`` age means a record briefly
    survives in S3 past its visibility window if no subsequent write
    triggers a sweep — that's fine, it's invisible to the operator.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAX_AGE_SECONDS)
    try:
        objects = s3.list_objects(FAILURE_PREFIX)
    except Exception:
        log.exception("failed to list failure breadcrumbs")
        return []
    records: list[FailureRecord] = []
    for obj in objects:
        key = obj["Key"]
        if not key.endswith(".json"):
            continue
        try:
            payload = json.loads(s3.get_object(key))
        except Exception:
            log.exception("failed to read failure breadcrumb %s", key)
            continue
        recorded_at = payload.get("recorded_at")
        ts = _parse_iso(recorded_at) if isinstance(recorded_at, str) else None
        if ts is None or ts < cutoff:
            continue
        records.append(
            FailureRecord(
                id=payload.get("id", ""),
                enqueued_at=payload.get("enqueued_at", ""),
                recorded_at=recorded_at,
                source=payload.get("source", ""),
                kind=payload.get("kind", ""),
                reason=payload.get("reason", ""),
                prompt=payload.get("prompt"),
            )
        )
    records.sort(key=lambda r: r.recorded_at, reverse=True)
    return records[:MAX_RECORDS_RETURNED]


def _sweep_expired() -> None:
    """Best-effort delete of breadcrumbs older than the visibility window.

    Uses S3 ``LastModified`` from the list response rather than reading
    each object's body — sweep runs on every failure write and the
    prefix is read-mostly otherwise.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAX_AGE_SECONDS)
    try:
        objects = s3.list_objects(FAILURE_PREFIX)
    except Exception:
        log.exception("failed to list failure breadcrumbs for sweep")
        return
    for obj in objects:
        last_modified = obj.get("LastModified")
        if last_modified is None:
            continue
        if last_modified.astimezone(timezone.utc) >= cutoff:
            continue
        try:
            s3.delete_object(obj["Key"])
        except Exception:
            log.exception(
                "failed to delete expired failure breadcrumb %s", obj["Key"]
            )
