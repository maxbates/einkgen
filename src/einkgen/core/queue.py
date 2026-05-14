"""S3-prefix-backed FIFO queue.

Items live at ``s3://<bucket>/queue/<iso8601>-<ulid>.json``. ULIDs are
lex-monotonic, so a sorted ``ListObjectsV2`` is FIFO order. The generator
Lambda runs with reserved concurrency = 1, which is what makes ``pop_head``
atomic enough — see README §4.

If we ever need multi-producer races, swap the implementation for SQS FIFO
or DynamoDB without changing this module's public API.
"""

from __future__ import annotations

import builtins
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3
from ulid import ULID

QUEUE_PREFIX = "queue/"
STAGED_PREFIX = "queue/staged/"


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


def _bucket() -> str:
    bucket = os.environ.get("EINKGEN_BUCKET")
    if not bucket:
        raise RuntimeError("EINKGEN_BUCKET env var is not set")
    return bucket


def _client():
    # boto3 picks up region/credentials from the environment / AWS_PROFILE.
    return boto3.client("s3")


def _iso_now() -> str:
    # Filename-safe ISO 8601 (colons replaced with hyphens) but JSON keeps the
    # canonical form.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key_timestamp(ts: str) -> str:
    return ts.replace(":", "-")


def _validate(kind: str, prompt: str | None, image_s3_key: str | None) -> None:
    if kind == "prompt":
        if not prompt:
            raise ValueError("kind='prompt' requires a prompt")
        if image_s3_key is not None:
            raise ValueError("kind='prompt' must not set image_s3_key")
    elif kind == "image":
        if not image_s3_key:
            raise ValueError("kind='image' requires image_s3_key")
        if prompt is not None:
            raise ValueError("kind='image' must not set prompt")
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
    _client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=json.dumps(item.to_json()).encode("utf-8"),
        ContentType="application/json",
    )
    return item


def _iter_pending_keys() -> list[str]:
    """Return lex-sorted queue object keys, excluding ``queue/staged/``."""
    client = _client()
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=QUEUE_PREFIX):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.startswith(STAGED_PREFIX):
                continue
            if not key.endswith(".json"):
                continue
            keys.append(key)
    keys.sort()
    return keys


def _read_item(key: str) -> QueueItem:
    client = _client()
    obj = client.get_object(Bucket=_bucket(), Key=key)
    payload = json.loads(obj["Body"].read())
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


def pop_head() -> QueueItem | None:
    keys = _iter_pending_keys()
    if not keys:
        return None
    head_key = keys[0]
    item = _read_item(head_key)
    _client().delete_object(Bucket=_bucket(), Key=head_key)
    return item


def cancel(item_id: str) -> bool:
    for key in _iter_pending_keys():
        if item_id in key:
            _client().delete_object(Bucket=_bucket(), Key=key)
            return True
    return False


def empty() -> bool:
    return not _iter_pending_keys()
