"""``einkgen-read-api`` Lambda — public, read-only state for the SPA.

Invoked via a Lambda **Function URL** (no API Gateway) using the
payload format v2.0:

    { "version": "2.0",
      "rawPath": "/queue",
      "queryStringParameters": {"limit": "10"},
      "requestContext": {"http": {"method": "GET", "path": "/queue"}},
      ... }

Routes (all GET):

- ``/queue``     → pending FIFO items from ``queue/`` (``queue/staged/`` excluded).
- ``/generated`` → pre-rendered frames waiting to be displayed (markers
  under ``generated/``). The buffer the ``/wake`` endpoint pops from.
- ``/history``   → recent ``history/<id>/manifest.json`` summaries, newest first.
- ``/status``    → newest ``status/device-<id>.json`` merged with the device id
  and S3 ``LastModified`` timestamp; 404 when no reports exist yet.
- ``/devices``   → every ``status/device-<id>.json`` merged with the
  device id and S3 ``LastModified``, newest first. Empty list when no
  reports exist. Lets the SPA list more than one device without each
  one having to alias to ``default``.

Response shape is ``{"statusCode": int, "headers": {...}, "body": json}``
so the Function URL serialises it directly. Real CORS is pinned by the
infra layer; the defensive header here keeps local dev workable.

The handler never raises: unexpected exceptions are logged and surfaced as
a 500 JSON body, because a Function URL otherwise returns Python tracebacks
verbatim to the client.
"""

from __future__ import annotations

import json
import logging
from datetime import timezone
from typing import Any

from einkgen.core import s3

log = logging.getLogger(__name__)

HISTORY_PREFIX = "history/"
STATUS_PREFIX = "status/"
QUEUE_PREFIX = "queue/"
STAGED_PREFIX = "queue/staged/"
GENERATED_PREFIX = "generated/"

DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 500
DEFAULT_QUEUE_LIMIT = 200
MAX_QUEUE_LIMIT = 1000
# Generated queue is target=10 in normal operation, but expose a higher
# cap so an SPA configured with a longer buffer in future doesn't get
# silently truncated.
DEFAULT_GENERATED_LIMIT = 50
MAX_GENERATED_LIMIT = 200
# Cap on the multi-device listing so a runaway producer or a leaked
# device token can't turn each public-SPA poll into O(devices) GetObjects.
DEFAULT_DEVICES_LIMIT = 50
MAX_DEVICES_LIMIT = 200

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def _response(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": dict(_BASE_HEADERS),
        "body": json.dumps(body),
    }


def _method(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    return str(method).upper()


def _path(event: dict[str, Any]) -> str:
    raw = event.get("rawPath")
    if isinstance(raw, str) and raw:
        return raw
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    path = http.get("path")
    if isinstance(path, str) and path:
        return path
    return "/"


def _normalize_path(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _parse_limit(
    event: dict[str, Any],
    *,
    default: int,
    maximum: int,
) -> int:
    params = event.get("queryStringParameters") or {}
    raw = params.get("limit") if isinstance(params, dict) else None
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, maximum)


def _handle_queue(event: dict[str, Any]) -> dict[str, Any]:
    # Bound the work per invocation so a wedged generator that lets the queue
    # grow unbounded can't make the dashboard timeout precisely when operators
    # need to see what's stuck.
    limit = _parse_limit(event, default=DEFAULT_QUEUE_LIMIT, maximum=MAX_QUEUE_LIMIT)
    keys = sorted(
        obj["Key"]
        for obj in s3.list_objects(QUEUE_PREFIX)
        if obj["Key"].endswith(".json")
        and not obj["Key"].startswith(STAGED_PREFIX)
    )
    items: list[dict[str, Any]] = []
    for key in keys[:limit]:
        try:
            items.append(json.loads(s3.get_object(key)))
        except Exception:
            # ERROR token so the CloudWatch metric filter catches the miss.
            log.error("ERROR unreadable queue item: %s", key)
            continue
    return _response(200, {"items": items})


def _handle_generated(event: dict[str, Any]) -> dict[str, Any]:
    """List the generated-queue markers in FIFO order (head first).

    Bounded by ``limit`` so a wedged buffer can't make the dashboard
    timeout. Each marker is already small (history_id + sha + source
    metadata) so we read every body in the range.
    """
    limit = _parse_limit(
        event,
        default=DEFAULT_GENERATED_LIMIT,
        maximum=MAX_GENERATED_LIMIT,
    )
    keys = sorted(
        obj["Key"]
        for obj in s3.list_objects(GENERATED_PREFIX)
        if obj["Key"].endswith(".json")
    )
    items: list[dict[str, Any]] = []
    for key in keys[:limit]:
        try:
            items.append(json.loads(s3.get_object(key)))
        except Exception:
            log.error("ERROR unreadable generated marker: %s", key)
            continue
    return _response(200, {"items": items})


def _handle_history(event: dict[str, Any]) -> dict[str, Any]:
    limit = _parse_limit(
        event, default=DEFAULT_HISTORY_LIMIT, maximum=MAX_HISTORY_LIMIT
    )
    manifest_keys = [
        obj["Key"]
        for obj in s3.list_objects(HISTORY_PREFIX)
        if obj["Key"].endswith("/manifest.json")
    ]
    # ULID ids are time-monotonic so the lex-sorted key list is already
    # newest-last; reverse-sort and read only the top `limit` manifests to
    # bound the per-request S3 read cost.
    manifest_keys.sort(reverse=True)

    entries: list[dict[str, Any]] = []
    for key in manifest_keys:
        if len(entries) >= limit:
            break
        try:
            data = json.loads(s3.get_object(key))
        except Exception:
            log.error("ERROR unreadable history manifest: %s", key)
            continue
        generated_at = data.get("generated_at", "")
        if not generated_at:
            # A backdated or malformed manifest with empty/missing
            # generated_at would sort to the bottom and dominate the tail;
            # drop it so it doesn't show up as a phantom entry.
            continue
        parts = key.split("/")
        item_id = parts[1] if len(parts) >= 3 else "?"
        entries.append(
            {
                "id": item_id,
                "generated_at": generated_at,
                "image_sha256": data.get("image_sha256", ""),
                "source": data.get("source", {}) or {},
            }
        )

    entries.sort(key=lambda e: e["generated_at"], reverse=True)
    return _response(200, {"items": entries})


_DEVICE_KEY_PREFIX = "status/device-"
_DEVICE_KEY_SUFFIX = ".json"


def _device_id_from_key(key: str) -> str:
    return key[len(_DEVICE_KEY_PREFIX) : -len(_DEVICE_KEY_SUFFIX)]


def _format_last_modified(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _device_records(limit: int | None = None) -> list[dict[str, Any]]:
    """Return ``status/device-<id>.json`` records, newest first.

    Each record carries the body fields plus ``device_id`` and a string
    ``last_modified`` so the SPA can render a stale-vs-fresh badge
    without re-deriving it.

    ``limit`` caps the number of GetObject calls; callers that don't
    care (the single-device ``/status`` path) leave it ``None`` to
    preserve the existing "newest one" semantics.
    """
    candidates = [
        obj
        for obj in s3.list_objects(STATUS_PREFIX)
        if obj["Key"].startswith(_DEVICE_KEY_PREFIX)
        and obj["Key"].endswith(_DEVICE_KEY_SUFFIX)
    ]
    candidates.sort(key=lambda o: o["LastModified"], reverse=True)
    if limit is not None:
        candidates = candidates[:limit]
    records: list[dict[str, Any]] = []
    for obj in candidates:
        try:
            report = json.loads(s3.get_object(obj["Key"]))
        except Exception:
            log.error("ERROR unreadable status report: %s", obj["Key"])
            continue
        record = dict(report) if isinstance(report, dict) else {}
        record["device_id"] = _device_id_from_key(obj["Key"])
        record["last_modified"] = _format_last_modified(obj["LastModified"])
        records.append(record)
    return records


def _handle_status() -> dict[str, Any]:
    # /status is the legacy single-device endpoint; only the newest
    # record is needed, so cap the read at 1 to avoid unbounded
    # GetObject costs even when many devices have reported.
    records = _device_records(limit=1)
    if not records:
        return _response(404, {"error": "no_status_yet"})
    return _response(200, records[0])


def _handle_devices(event: dict[str, Any]) -> dict[str, Any]:
    limit = _parse_limit(
        event, default=DEFAULT_DEVICES_LIMIT, maximum=MAX_DEVICES_LIMIT
    )
    return _response(200, {"items": _device_records(limit=limit)})


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    try:
        method = _method(event)
        path = _normalize_path(_path(event))

        if method != "GET":
            return _response(405, {"error": "method_not_allowed"})

        if path == "/queue":
            return _handle_queue(event)
        if path == "/generated":
            return _handle_generated(event)
        if path == "/history":
            return _handle_history(event)
        if path == "/status":
            return _handle_status()
        if path == "/devices":
            return _handle_devices(event)
        return _response(404, {"error": "not_found"})
    except Exception:
        log.exception("read_api unhandled error")
        return _response(500, {"error": "internal"})
