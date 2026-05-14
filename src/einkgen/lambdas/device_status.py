"""``einkgen-device-status`` Lambda — accepts the Inkplate's wake-time status POST.

Invocation
----------
Lambda Function URL (payload format v2.0). The firmware POSTs JSON with an
``X-Device-Token`` header on every wake (see ``firmware/inkplate10/inkplate10.ino``
``postStatus`` — it sends ``battery_v``, ``battery_pct``, ``rssi``,
``current_hash``, ``fw_version``).

Auth
----
Shared secret. The header is validated against the raw ``SecretString`` of an
AWS Secrets Manager secret (no JSON wrapping). Wrong/missing token → 401 with
no S3 write — the threat model in README §16 treats unauthenticated POSTs as
S3-cost griefing. Comparison uses ``hmac.compare_digest`` for constant time.
The fetched token is cached at module scope so warm invocations skip the
Secrets Manager API call.

Storage
-------
Writes ``status/device-<device_id>.json`` with the body fields plus a
server-side ``last_seen`` ISO 8601 UTC timestamp. One key per device, latest
wins — historical retention is deferred (see README §14).

device_id resolution
--------------------
The current firmware revision (``firmware/inkplate10/inkplate10.ino``) does
**not** send ``device_id`` in its POST body. TODO: extend firmware to include
``device_id`` (e.g. derived from the ESP32 MAC or a value baked into
``secrets.h``). Until then we resolve ``device_id`` via the simplest path that
won't bounce real traffic:

  1. body ``device_id`` if present;
  2. otherwise the literal string ``"default"``.

We deliberately do NOT key on ``X-Forwarded-For`` — the read-api Device tab
expects a stable key, and a residential IP changes more often than a device's
identity. Single-device deployments collapse onto ``status/device-default.json``
without breaking anything; the moment firmware starts sending ``device_id``,
the new key appears alongside it and the old one ages out naturally.

IAM (for Track D infra)
-----------------------
The Lambda execution role needs:

  - ``secretsmanager:GetSecretValue`` on the configured secret's ARN
    (env ``DEVICE_STATUS_SECRET_NAME``, default ``einkgen/device_status_token``).
  - ``s3:PutObject`` on ``arn:aws:s3:::<bucket>/status/*`` only (write-only,
    scoped to the prefix — see README §8 access policy).

No other permissions are required: this Lambda never reads S3, never invokes
other services, and emits logs via the default CloudWatch Logs role.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

from einkgen.core import s3 as s3mod

log = logging.getLogger(__name__)

DEFAULT_SECRET_NAME = "einkgen/device_status_token"
DEFAULT_DEVICE_ID = "default"

# Constant headers on every response. CORS is set defensively here so the
# Lambda is usable in isolation; the real CORS pin lives on the Function URL
# itself (Track D infra).
_RESPONSE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}

# Module-scope token cache. Lambda reuses warm execution environments across
# invocations; caching here means ~one Secrets Manager call per cold start
# instead of one per device wake.
_cached_token: str | None = None
_sm_client = None


def _get_sm_client():
    global _sm_client
    if _sm_client is None:
        _sm_client = boto3.client("secretsmanager")
    return _sm_client


def _reset_cache() -> None:
    """Drop the cached token + Secrets Manager client. Used by tests."""
    global _cached_token, _sm_client
    _cached_token = None
    _sm_client = None


def _expected_token() -> str:
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    secret_name = os.environ.get("DEVICE_STATUS_SECRET_NAME", DEFAULT_SECRET_NAME)
    resp = _get_sm_client().get_secret_value(SecretId=secret_name)
    # SecretString stored as a raw token — no JSON unwrap. Matches the
    # convention in README §10 and keeps the Terraform secret resource trivial.
    _cached_token = resp["SecretString"]
    return _cached_token


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": dict(_RESPONSE_HEADERS),
        "body": json.dumps(payload),
    }


def _header(headers: dict[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    # Function URLs lowercase header keys before invoking us, but a direct
    # caller (local test, curl) may pass any casing — scan case-insensitively
    # so we don't gate auth on a Function-URL-specific quirk.
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _now_iso() -> str:
    # ISO 8601 UTC with explicit Z suffix (matches the manifest format in §7).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    ).upper()
    if method != "POST":
        return _response(400, {"error": "bad_request", "detail": "expected POST"})

    headers = event.get("headers") or {}
    provided = _header(headers, "x-device-token")
    if not provided:
        return _response(401, {"error": "unauthorized"})

    try:
        expected = _expected_token()
    except Exception:
        # If Secrets Manager is unreachable we cannot authenticate the caller;
        # treat as 401 rather than 500 so an attacker can't distinguish "wrong
        # token" from "service degraded" and we never accidentally write.
        log.exception("failed to fetch device status secret")
        return _response(401, {"error": "unauthorized"})

    if not hmac.compare_digest(provided, expected):
        return _response(401, {"error": "unauthorized"})

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception as exc:
            return _response(
                400, {"error": "bad_request", "detail": f"invalid base64: {exc}"}
            )

    if not raw_body:
        return _response(400, {"error": "bad_request", "detail": "empty body"})

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return _response(
            400, {"error": "bad_request", "detail": f"invalid json: {exc.msg}"}
        )

    if not isinstance(body, dict):
        return _response(
            400, {"error": "bad_request", "detail": "body must be a JSON object"}
        )

    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id:
        device_id = DEFAULT_DEVICE_ID

    record = dict(body)
    record["device_id"] = device_id
    record["last_seen"] = _now_iso()

    key = f"status/device-{device_id}.json"
    s3mod.put_object(
        key,
        json.dumps(record).encode("utf-8"),
        content_type="application/json",
    )

    return _response(200, {"ok": True, "device_id": device_id})
