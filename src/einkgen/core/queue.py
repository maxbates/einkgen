"""S3-prefix-backed FIFO queue.

Items live at ``s3://<bucket>/queue/<iso8601>-<ulid>.json``. ULIDs are
lex-monotonic, so a sorted ``ListObjectsV2`` is FIFO order. The generator
Lambda runs with reserved concurrency = 1, which is what makes head reads
race-free — see ARCHITECTURE §4.

If we ever need multi-producer races, swap the implementation for SQS FIFO
or DynamoDB without changing this module's public API.
"""

from __future__ import annotations

import builtins
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ulid import ULID

from einkgen.core import s3

QUEUE_PREFIX = "queue/"
STAGED_PREFIX = "queue/staged/"
# Operator-visible breadcrumbs for permanently-failed items live under this
# prefix. Excluded from queue listing so the public Queue tab never shows
# dropped items. See ``einkgen.core.failures``.
FAILED_PREFIX = "queue/failed/"


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
) -> QueueItem:
    """Write a new queue item to S3 and return it."""
    _validate(kind, prompt, image_s3_key)
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
    key = f"{QUEUE_PREFIX}{_key_timestamp(enqueued_at)}-{item_id}.json"
    item._s3_key = key
    s3.put_object(
        key,
        json.dumps(item.to_json()).encode("utf-8"),
        content_type="application/json",
    )
    return item


def _iter_pending_keys() -> list[str]:
    """Return lex-sorted queue object keys.

    Excludes ``queue/staged/`` (raw uploads waiting for a queue item) and
    ``queue/failed/`` (operator-visible drop breadcrumbs) — both share the
    ``queue/`` prefix for IAM/lifecycle simplicity but are not pending
    work.
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
    """Return all pending queue items in FIFO order."""
    return [_read_item(k) for k in _iter_pending_keys()]


def peek_head() -> QueueItem | None:
    """Return the FIFO head without deleting it.

    Pair with ``finalize(item)`` after the item has been processed
    successfully. If processing raises, the item stays on the queue and
    Lambda's async-invocation retry will redeliver the S3 event.
    """
    keys = _iter_pending_keys()
    if not keys:
        return None
    return _read_item(keys[0])


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
