"""``einkgen-read-api`` Lambda — public, read-only state for the SPA.

Invoked via a Lambda **Function URL** (no API Gateway) using the
payload format v2.0:

    { "version": "2.0",
      "rawPath": "/queue",
      "queryStringParameters": {"limit": "10"},
      "requestContext": {"http": {"method": "GET", "path": "/queue"}},
      ... }

Routes (all GET):

- ``/queue``   → pending FIFO items from ``queue/`` (``queue/staged/`` excluded).
- ``/history`` → recent ``history/<id>/manifest.json`` summaries, newest first.
- ``/status``  → newest ``status/device-<id>.json`` merged with the device id
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
from typing import Any

from einkgen.core import queue, s3

log = logging.getLogger(__name__)

HISTORY_PREFIX = "history/"
STATUS_PREFIX = "status/"

DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 500

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


def _parse_limit(event: dict[str, Any]) -> int:
    params = event.get("queryStringParameters") or {}
    raw = params.get("limit") if isinstance(params, dict) else None
    if raw is None:
        return DEFAULT_HISTORY_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT
    if n <= 0:
        return DEFAULT_HISTORY_LIMIT
    return min(n, MAX_HISTORY_LIMIT)


def _handle_queue() -> dict[str, Any]:
    items = [item.to_json() for item in queue.list()]
    return _response(200, {"items": items})


def _handle_history(event: dict[str, Any]) -> dict[str, Any]:
    limit = _parse_limit(event)
    manifest_keys = [
        obj["Key"]
        for obj in s3.list_objects(HISTORY_PREFIX)
        if obj["Key"].endswith("/manifest.json")
    ]

    entries: list[dict[str, Any]] = []
    for key in manifest_keys:
        try:
            data = json.loads(s3.get_object(key))
        except Exception:
            # A single broken manifest must not poison the whole listing.
            log.warning("skipping unreadable history manifest: %s", key)
            continue
        parts = key.split("/")
        item_id = parts[1] if len(parts) >= 3 else "?"
        entries.append(
            {
                "id": item_id,
                "generated_at": data.get("generated_at", ""),
                "image_sha256": data.get("image_sha256", ""),
                "source": data.get("source", {}) or {},
            }
        )

    entries.sort(key=lambda e: e["generated_at"], reverse=True)
    return _response(200, {"items": entries[:limit]})


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
    if hasattr(last_modified, "isoformat"):
        last_modified_str = last_modified.isoformat()
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
            return _handle_queue()
        if path == "/history":
            return _handle_history(event)
        if path == "/status":
            return _handle_status()
        return _response(404, {"error": "not_found"})
    except Exception:
        log.exception("read_api unhandled error")
        return _response(500, {"error": "internal"})
