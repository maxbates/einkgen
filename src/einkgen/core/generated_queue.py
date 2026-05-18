"""FIFO buffer of pre-rendered frames waiting to be displayed.

Sits between the prompt queue (text/image submissions waiting to be
rendered) and the history archive (everything ever published). Each
marker here points at an existing ``history/<id>/`` archive — the
bytes have already been generated, dithered, and stashed; the device
just hasn't drawn them yet.

The /wake endpoint is what advances the display: it pops the head
marker and re-points ``current/manifest.json`` at the corresponding
history frame. Cron tops the buffer back up to
``TARGET_GENERATED_QUEUE_LENGTH`` by feeding prompts from the prompt
queue through the image model. Admin can **Skip** a marker (delete
without ever displaying) or **Show this now** (advance to a specific
generated item).

Storage
-------
Each marker lives at:

    s3://<bucket>/generated/<iso8601>-<history_id>.json

Single FIFO — no priorities, no in-place mutation. Lex-sorted
``ListObjectsV2`` is the queue order (oldest-first). Each marker
embeds the fields the SPA needs to render a tile without an extra
fetch per item:

    {
      "history_id": "01HF7Z…",
      "queued_at": "2026-05-13T14:05:12Z",
      "image_sha256": "abc…",
      "image_bytes": 990123,
      "source": {"kind": "generated", "model": "gpt-image-2",
                  "prompt": "…"}
    }

History id IS the canonical id of a marker — there's a one-to-one
correspondence between an entry in this queue and a folder under
``history/``. Admin actions (skip / show) reference items by
history_id.
"""

from __future__ import annotations

import builtins
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from einkgen.core import s3

GENERATED_PREFIX = "generated/"


@dataclass
class GeneratedItem:
    history_id: str
    queued_at: str
    image_sha256: str
    image_bytes: int
    source: dict[str, Any] = field(default_factory=dict)
    # Internal: full S3 key, set when retrieved (not serialised on write).
    _s3_key: str | None = field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_s3_key", None)
        return data


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key_timestamp(ts: str) -> str:
    return ts.replace(":", "-")


def enqueue(
    history_id: str,
    *,
    image_sha256: str,
    image_bytes: int,
    source: dict[str, Any] | None = None,
    queued_at: str | None = None,
) -> GeneratedItem:
    """Append a new marker to the bottom of the generated queue."""
    if not history_id:
        raise ValueError("history_id is required")
    queued_at = queued_at or _iso_now()
    item = GeneratedItem(
        history_id=history_id,
        queued_at=queued_at,
        image_sha256=image_sha256,
        image_bytes=image_bytes,
        source=dict(source or {}),
    )
    key = f"{GENERATED_PREFIX}{_key_timestamp(queued_at)}-{history_id}.json"
    item._s3_key = key
    s3.put_object(
        key,
        json.dumps(item.to_json()).encode("utf-8"),
        content_type="application/json",
    )
    return item


def _iter_marker_keys() -> list[str]:
    """Lex-sorted marker keys = FIFO order (oldest first)."""
    keys: list[str] = []
    for obj in s3.list_objects(GENERATED_PREFIX):
        key = obj["Key"]
        if not key.endswith(".json"):
            continue
        keys.append(key)
    keys.sort()
    return keys


def _read_item(key: str) -> GeneratedItem:
    payload = json.loads(s3.get_object(key))
    item = GeneratedItem(
        history_id=payload["history_id"],
        queued_at=payload["queued_at"],
        image_sha256=payload.get("image_sha256", ""),
        image_bytes=int(payload.get("image_bytes", 0) or 0),
        source=payload.get("source", {}) or {},
    )
    item._s3_key = key
    return item


def list() -> builtins.list[GeneratedItem]:  # noqa: A001 — mirrors queue.list()
    """All markers in FIFO order (head first)."""
    return [_read_item(k) for k in _iter_marker_keys()]


def peek_head() -> GeneratedItem | None:
    keys = _iter_marker_keys()
    if not keys:
        return None
    return _read_item(keys[0])


def get(history_id: str) -> GeneratedItem | None:
    """Fetch a marker by its history_id, or ``None``.

    Matches on the trailing ``-<id>.json`` suffix so a stray partial id
    can't accidentally match an unrelated marker.
    """
    suffix = f"-{history_id}.json"
    for key in _iter_marker_keys():
        if key.endswith(suffix):
            return _read_item(key)
    return None


def finalize(item: GeneratedItem) -> None:
    """Delete a marker that's been consumed (popped + displayed)."""
    if item._s3_key is None:
        raise ValueError("GeneratedItem has no S3 key; was it returned by peek_head?")
    s3.delete_object(item._s3_key)


def cancel(history_id: str) -> bool:
    """Drop a marker by id (admin skip). Returns whether one was deleted.

    Idempotent: a missing marker returns ``False`` without raising, so
    racing skip clicks or a skip-after-pop don't surface as errors.
    """
    suffix = f"-{history_id}.json"
    for key in _iter_marker_keys():
        if key.endswith(suffix):
            s3.delete_object(key)
            return True
    return False


def count() -> int:
    return len(_iter_marker_keys())


def empty() -> bool:
    return not _iter_marker_keys()
