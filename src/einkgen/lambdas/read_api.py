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


def _handle_status() -> dict[str, Any]:
    candidates = [
        obj
        for obj in s3.list_objects(STATUS_PREFIX)
        if obj["Key"].startswith("status/device-")
        and obj["Key"].endswith(".json")
    ]
    if not candidates:
        return _response(404, {"error": "no_status_yet"})

    latest = max(candidates, key=lambda o: o["LastModified"])
    report = json.loads(s3.get_object(latest["Key"]))

    # Extract `<id>` from `status/device-<id>.json`.
    device_id = latest["Key"][len("status/device-") : -len(".json")]
    last_modified = latest["LastModified"]
    if hasattr(last_modified, "strftime"):
        # Match the Z-suffix convention used by manifest/device_status.
        last_modified_str = last_modified.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        last_modified_str = str(last_modified)

    body = dict(report)
    body["device_id"] = device_id
    body["last_modified"] = last_modified_str
    return _response(200, body)


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
        return _response(404, {"error": "not_found"})
    except Exception:
        log.exception("read_api unhandled error")
        return _response(500, {"error": "internal"})
