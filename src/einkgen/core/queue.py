"""Two-priority S3-prefix queue.

Items live at:

    s3://<bucket>/queue/<priority>-<iso8601>-<ulid>.json

``<priority>`` is the literal character ``"0"`` (top queue) or ``"1"``
(bottom queue). Lex-sorted ``ListObjectsV2`` is the queue order: all
top-priority items drain before any bottom-priority item, oldest-first
within each priority.

There is no in-place mutation of queue objects. To "promote" an item to
the top, callers don't reorder — instead, the generator can be invoked
with ``{"action": "render_item", "item_id": ...}`` to render that
specific item out of order. Removal stays the same suffix-match cancel.

Why two queues, not a single ordered list?
------------------------------------------
The earlier design used a ``position: float`` field inside each JSON
body and supported arbitrary reordering. Operating it required reading
+ rewriting individual queue objects to move them around, which the
user found brittle for a buffer that an operator might tweak from a
phone. Two priorities give the SPA the **Top** / **Bottom** / **Now**
UX (same as Apple Music's Play Next / Play Last / Play Now) without
any S3 object ever being mutated after it's written.

Legacy item handling
--------------------
Items enqueued by code older than this rewrite have keys of the form
``queue/<iso8601>-<ulid>.json`` (no priority prefix). They lex-sort
*after* both new priorities — ``"2026-…"`` is greater than both
``"0-…"`` and ``"1-…"`` — so they get drained as the tail of the queue
over a handful of cron ticks. No migration needed; they just trickle
out.

If we ever outgrow S3-prefix queues (multi-producer races, high write
rate), swap this module for SQS FIFO or DynamoDB without changing the
public API.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ulid import ULID

from einkgen.core import s3

QUEUE_PREFIX = "queue/"
STAGED_PREFIX = "queue/staged/"

# Allowlist of image-file extensions kept on the staged key. Staged
# uploads are always images (the queue rejects non-image kinds), and
# limiting the suffix to a known set keeps the public CDN URL from ever
# advertising e.g. ``.sh`` or ``.exe`` even if an operator hands one in.
_STAGED_ALLOWED_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp"}
)


def build_staged_key(data: bytes, filename: str | None = None) -> str:
    """Return ``queue/staged/<sha8><ext>`` for a freshly-uploaded image.

    Older revisions baked the operator-supplied filename into the key.
    That leaked through the public CloudFront ``queue/staged/*``
    behavior — for email submissions the leaked string could be a
    sender's local file path. The new key keeps only the content hash
    and a safe extension so the CDN URL reveals nothing about the
    submitter.
    """
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    ext = ""
    if filename:
        _, raw_ext = os.path.splitext(filename)
        candidate = raw_ext.lower()
        if candidate in _STAGED_ALLOWED_EXTS:
            ext = candidate
    return f"{STAGED_PREFIX}{sha8}{ext}"


# Operator-visible breadcrumbs for permanently-failed items live under
# this prefix. Excluded from queue listing so the public Queue tab never
# shows dropped items. See ``einkgen.core.failures`` (introduced in
# [0.4.1.4]).
FAILED_PREFIX = "queue/failed/"

# Priority characters. Single ASCII digits so the lex sort of the
# concatenated key is the queue order: "0-..." < "1-...".
PRIORITY_TOP = "0"
PRIORITY_BOTTOM = "1"
_VALID_PRIORITIES = (PRIORITY_TOP, PRIORITY_BOTTOM)

# Map the user-facing placement words to the on-disk priority chars.
_PRIORITY_FOR_AT = {
    "top": PRIORITY_TOP,
    "bottom": PRIORITY_BOTTOM,
}


@dataclass
class QueueItem:
    id: str
    enqueued_at: str
    source: str
    kind: str
    prompt: str | None = None
    image_s3_key: str | None = None
    # Internal: the full S3 key, set when retrieved (not serialised).
    _s3_key: str | None = field(default=None, repr=False)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_s3_key", None)
        return data


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key_timestamp(ts: str) -> str:
    # Colons aren't legal in S3 keys for some downstream tools; replace with -.
    return ts.replace(":", "-")


def _validate(kind: str, prompt: str | None, image_s3_key: str | None) -> None:
    if kind == "prompt":
        if not prompt:
            raise ValueError("kind='prompt' requires a prompt")
        if image_s3_key is not None:
            raise ValueError("kind='prompt' must not set image_s3_key")
    elif kind == "image":
        # kind='image' accepts an optional prompt. With no prompt we treat the
        # upload as "convert to B&W and publish". With a prompt we feed both
        # to gpt-image-2's edit endpoint so the prompt restyles the image.
        if not image_s3_key:
            raise ValueError("kind='image' requires image_s3_key")
    elif kind == "random":
        # Legacy kind kept for back-compat with items already on the queue.
        # New code paths (cron, admin) emit kind="prompt" with an
        # LLM-expanded subject instead — see einkgen.lambdas.generator.
        if prompt is not None or image_s3_key is not None:
            raise ValueError("kind='random' must not set prompt or image_s3_key")
    else:
        raise ValueError(f"unknown kind: {kind!r}")


def enqueue(
    kind: str,
    *,
    prompt: str | None = None,
    image_s3_key: str | None = None,
    source: str = "cli",
    at: str = "bottom",
) -> QueueItem:
    """Write a new queue item to S3 and return it.

    ``at`` selects which priority queue the item lands in:
    ``"bottom"`` (default) or ``"top"``. There is no reordering after
    the fact — pick the right placement at enqueue time.
    """
    _validate(kind, prompt, image_s3_key)
    if at not in _PRIORITY_FOR_AT:
        raise ValueError(f"at must be 'top' or 'bottom', got {at!r}")
    priority = _PRIORITY_FOR_AT[at]

    item_id = str(ULID())
    enqueued_at = _iso_now()
    item = QueueItem(
        id=item_id,
        enqueued_at=enqueued_at,
        source=source,
        kind=kind,
        prompt=prompt,
        image_s3_key=image_s3_key,
    )
    key = f"{QUEUE_PREFIX}{priority}-{_key_timestamp(enqueued_at)}-{item_id}.json"
    item._s3_key = key
    s3.put_object(
        key,
        json.dumps(item.to_json()).encode("utf-8"),
        content_type="application/json",
    )
    return item


def _iter_pending_keys() -> list[str]:
    """Return lex-sorted queue object keys.

    Lex sort happens to be queue order: "0-..." < "1-..." < "2026-..."
    (legacy items lacking a priority prefix sort after both priorities,
    so they drain last). No need to parse the key for ordering.

    Excludes ``queue/staged/`` (raw uploads waiting for a queue item)
    and ``queue/failed/`` (operator-visible drop breadcrumbs) — both
    share the ``queue/`` prefix for IAM/lifecycle simplicity but are
    not pending work.
    """
    keys: list[str] = []
    for obj in s3.list_objects(QUEUE_PREFIX):
        key = obj["Key"]
        if key.startswith(STAGED_PREFIX):
            continue
        if key.startswith(FAILED_PREFIX):
            continue
        if not key.endswith(".json"):
            continue
        keys.append(key)
    keys.sort()
    return keys


def _read_item(key: str) -> QueueItem:
    payload = json.loads(s3.get_object(key))
    item = QueueItem(
        id=payload["id"],
        enqueued_at=payload["enqueued_at"],
        source=payload["source"],
        kind=payload["kind"],
        prompt=payload.get("prompt"),
        image_s3_key=payload.get("image_s3_key"),
    )
    item._s3_key = key
    return item


def list() -> builtins.list[QueueItem]:  # noqa: A001 — name dictated by spec
    """Return all pending queue items in queue order (top first, then bottom)."""
    return [_read_item(k) for k in _iter_pending_keys()]


def peek_head() -> QueueItem | None:
    """Return the queue head without deleting it.

    Top-priority items always come before bottom-priority items;
    within each priority, FIFO by enqueue timestamp.

    Pair with ``finalize(item)`` after the item has been processed
    successfully. If processing raises, the item stays on the queue.
    """
    keys = _iter_pending_keys()
    if not keys:
        return None
    return _read_item(keys[0])


def get(item_id: str) -> QueueItem | None:
    """Fetch a single item by id, or ``None`` if not found.

    Matches on the trailing ``-<id>.json`` suffix so a stray partial
    id can't accidentally match an unrelated item.
    """
    suffix = f"-{item_id}.json"
    for key in _iter_pending_keys():
        if key.endswith(suffix):
            return _read_item(key)
    return None


def finalize(item: QueueItem) -> None:
    """Delete a queue item after it has been processed successfully.

    Safe to call twice (idempotent): a missing key is treated as already
    finalized. Re-deliveries don't need to special-case this.
    """
    if item._s3_key is None:
        raise ValueError("QueueItem has no S3 key; was it returned by peek_head?")
    s3.delete_object(item._s3_key)


def cancel(item_id: str) -> bool:
    """Delete a pending item by its id.

    Matches on the trailing ``-<id>.json`` suffix so a stray partial id
    can't accidentally delete an unrelated item. Returns whether
    something was deleted.
    """
    suffix = f"-{item_id}.json"
    for key in _iter_pending_keys():
        if key.endswith(suffix):
            s3.delete_object(key)
            return True
    return False


def empty() -> bool:
    return not _iter_pending_keys()


def count() -> int:
    """How many pending items the queue currently holds."""
    return len(_iter_pending_keys())
