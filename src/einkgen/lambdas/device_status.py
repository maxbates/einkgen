"""``einkgen-device-status`` Lambda — firmware-facing status + wake endpoints.

Two POST routes, both shared-secret authenticated via ``X-Device-Token``:

- ``POST /`` — the periodic status heartbeat. The firmware sends this
  on every wake with ``battery_v`` / ``battery_pct`` / ``rssi`` /
  ``current_hash`` / ``fw_version``. Persisted at
  ``status/device-<id>.json``; consumed by the SPA Device tab.

- ``POST /wake`` — the wake-driven display advance. Body
  ``{"current_sha256": "<hex>"}`` reports what the panel is currently
  showing. The handler compares against ``current/manifest.json``:

      * sha mismatch  → ``{"action":"redraw", ...manifest fields}``.
        The device just hasn't drawn the latest manifest yet. We do
        NOT pop the generated queue here — debouncing falls naturally
        out of this check, so rapid presses don't burn through the
        buffer.

      * sha match + generated queue non-empty → pop the head marker,
        re-point ``current/manifest.json`` at that history frame,
        async-invoke the generator with ``render_one`` to backfill,
        respond ``{"action":"advance", ...manifest fields}``.

      * sha match (or no manifest yet) + generated queue empty →
        ``{"action":"queue_empty"}``. Don't burn a synchronous OpenAI
        call to invent a fresh frame — the next cron tick will refill
        the buffer. No manifest fields needed; the firmware keeps
        drawing what it already has.

  Advance and redraw responses embed ``image_url``, ``image_sha256``,
  ``image_bytes`` and ``next_check_after`` so the firmware can skip
  the follow-up ``GET current/manifest.json`` entirely. CloudFront
  caches that path for 60–300 s and a fresh
  ``CreateInvalidation`` typically takes 5–60 s to propagate, so
  fetching it right after a server-side advance reliably returns the
  pre-advance manifest. Embedding the fields collapses the wake
  round-trip to one POST and one image GET.

Auth
----
Shared secret. The header is validated against the raw ``SecretString`` of an
AWS Secrets Manager secret (no JSON wrapping). Wrong/missing token → 401 with
no S3 write — the threat model in ARCHITECTURE §12 treats unauthenticated POSTs as
S3-cost griefing. Comparison uses ``hmac.compare_digest`` for constant time.
The fetched token is cached at module scope so warm invocations skip the
Secrets Manager API call.

Status storage
--------------
Writes ``status/device-<device_id>.json`` with the body fields plus a
server-side ``last_seen`` ISO 8601 UTC timestamp. One key per device, latest
wins — historical retention is deferred (see PLAN §3).

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

IAM (for CDK)
-------------
The Lambda execution role needs:

  - ``secretsmanager:GetSecretValue`` on the configured secret's ARN
    (env ``DEVICE_STATUS_SECRET_NAME``, default ``einkgen/device_status_token``).
  - ``s3:PutObject`` on ``arn:aws:s3:::<bucket>/status/*``.
  - ``s3:GetObject``/``s3:PutObject`` on ``current/manifest.json`` for
    the ``/wake`` advance.
  - ``s3:GetObject`` on ``history/*/manifest.json`` (read history
    metadata when advancing).
  - ``s3:GetObject``/``s3:DeleteObject`` on ``generated/*`` plus
    ``s3:ListBucket`` scoped to ``generated/*`` (read + pop markers).
  - ``cloudfront:CreateInvalidation`` (``set_current_from_history``
    invalidates ``/current/manifest.json``).
  - ``lambda:InvokeFunction`` on the generator Lambda (replenish via
    ``render_one`` after a successful advance).

Reserved concurrency is set in CDK; the race window between two wake
calls popping the same marker is rare in practice (one device + 30-min
timer cadence + occasional button press) and the failure mode is
benign — both pop attempts call ``set_current_from_history`` on the
same id and only one ``delete_object`` succeeds.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from einkgen.core import generated_queue, publish
from einkgen.core import s3 as s3mod
from einkgen.core.manifest import Manifest

log = logging.getLogger(__name__)

DEFAULT_SECRET_NAME = "einkgen/device_status_token"
DEFAULT_DEVICE_ID = "default"

# Real firmware payload is <200 bytes. 4 KB leaves room for additions while
# bounding what a token-holder can stuff into status/. Larger bodies are
# rejected before json.loads so we never serialise junk to S3.
MAX_BODY_BYTES = 4 * 1024

# Re-fetch the token after this many seconds. Bounds how long a rotated secret
# stays unreachable to warm Lambda containers (ARCHITECTURE §10 says rotate on
# suspicion — without a TTL, leaked tokens keep working on warm containers
# until they recycle, which can be hours).
TOKEN_CACHE_TTL_SECONDS = 300

# Explicit body allowlist. Anything not on this list is dropped before we
# serialise the record to S3 — a token-holder can't stuff arbitrary fields
# into status/, can't blow up the SPA with `(1e100).toFixed(2)`, etc.
ALLOWED_BODY_FIELDS: frozenset[str] = frozenset(
    {"battery_v", "battery_pct", "rssi", "current_hash", "fw_version"}
)

# device_id flows into an S3 key, so constrain it to a safe charset.
# Permissive enough for ULIDs, UUIDs, MACs (with or without separators),
# and human-readable labels like "kitchen".
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# A reported current_sha256 must look like a 64-char lowercase hex digest
# (what we write into manifests). Reject anything else before comparing
# so a token-holder can't dump arbitrary bytes through ``current_sha256``.
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# Constant headers on every response. CORS is set defensively here so the
# Lambda is usable in isolation; the real CORS pin lives on the HTTP API
# itself (no CORS configured — device-status is firmware-only).
_RESPONSE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}

# Module-scope token cache with a TTL. Lambda reuses warm execution
# environments across invocations; caching means ~one Secrets Manager call
# per cold start. The TTL caps how long a rotated secret stays unreachable
# (see TOKEN_CACHE_TTL_SECONDS comment above).
_cached_token: str | None = None
_cached_token_at: float = 0.0
_sm_client = None
_lambda_client = None


def _get_sm_client():
    global _sm_client
    if _sm_client is None:
        _sm_client = boto3.client("secretsmanager")
    return _sm_client


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _reset_cache() -> None:
    """Drop the cached token + Secrets Manager client. Used by tests."""
    global _cached_token, _cached_token_at, _sm_client, _lambda_client
    _cached_token = None
    _cached_token_at = 0.0
    _sm_client = None
    _lambda_client = None


def _expected_token() -> str:
    global _cached_token, _cached_token_at
    now = time.monotonic()
    if _cached_token is not None and (now - _cached_token_at) < TOKEN_CACHE_TTL_SECONDS:
        return _cached_token
    secret_name = os.environ.get("DEVICE_STATUS_SECRET_NAME", DEFAULT_SECRET_NAME)
    resp = _get_sm_client().get_secret_value(SecretId=secret_name)
    # SecretString stored as a raw token — no JSON unwrap. Matches the
    # convention in ARCHITECTURE §10 and keeps the secret resource trivial.
    # A binary-only secret would lack SecretString; treat that as misconfig
    # rather than silently caching None.
    if "SecretString" not in resp:
        raise RuntimeError(
            "device_status_token secret has no SecretString; "
            "use --secret-string (not --secret-binary) when rotating."
        )
    _cached_token = resp["SecretString"]
    _cached_token_at = now
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
    # HTTP API lowercases header keys before invoking us, but a direct
    # caller (local test, curl) may pass any casing — scan case-insensitively
    # so we don't gate auth on a platform-specific quirk.
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _now_iso() -> str:
    # ISO 8601 UTC with explicit Z suffix (matches the manifest format in §7).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_method(event: dict[str, Any]) -> str:
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    )
    return str(method).upper()


def _request_path(event: dict[str, Any]) -> str:
    raw = event.get("rawPath")
    if isinstance(raw, str) and raw:
        path = raw
    else:
        ctx = event.get("requestContext") or {}
        http = ctx.get("http") or {}
        path = http.get("path", "/")
    if not isinstance(path, str) or not path:
        return "/"
    # Trim trailing slash on multi-char paths so /wake and /wake/ are the same.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _decode_body(event: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Return ``(raw_body, error_response)``."""
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception as exc:
            return None, _response(
                400, {"error": "bad_request", "detail": f"invalid base64: {exc}"}
            )
    if not raw_body:
        return None, _response(400, {"error": "bad_request", "detail": "empty body"})
    if len(raw_body) > MAX_BODY_BYTES:
        return None, _response(
            413, {"error": "bad_request", "detail": "body too large"}
        )
    return raw_body, None


def _authorize(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return None on success, an error response dict on failure."""
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
    return None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    method = _request_method(event)
    path = _request_path(event)

    if method != "POST":
        return _response(400, {"error": "bad_request", "detail": "expected POST"})

    if path in ("/", ""):
        return _handle_status(event)
    if path == "/wake":
        return _handle_wake(event)
    return _response(404, {"error": "not_found"})


def _handle_status(event: dict[str, Any]) -> dict[str, Any]:
    """``POST /`` — persist the device's latest battery/RSSI heartbeat."""
    auth_err = _authorize(event)
    if auth_err is not None:
        return auth_err

    raw_body, err = _decode_body(event)
    if err is not None:
        return err
    assert raw_body is not None

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
    elif not DEVICE_ID_RE.match(device_id):
        # Stop here rather than silently falling back — a token-holder
        # supplying junk should see the failure, not have it relabelled.
        return _response(
            400, {"error": "bad_request", "detail": "invalid device_id"}
        )

    # Build the record from an explicit allowlist so a token-holder can't
    # stuff extra fields (huge strings, `1e100` numbers, etc.) into status/.
    record: dict[str, Any] = {
        k: body[k] for k in ALLOWED_BODY_FIELDS if k in body
    }
    record["device_id"] = device_id
    record["last_seen"] = _now_iso()

    key = f"status/device-{device_id}.json"
    s3mod.put_object(
        key,
        json.dumps(record).encode("utf-8"),
        content_type="application/json",
    )

    return _response(200, {"ok": True, "device_id": device_id})


_MISSING_OBJECT_CODES = {"NoSuchKey", "NotFound", "404"}


def _read_current_manifest() -> Manifest | None:
    """Return the parsed ``current/manifest.json`` or None.

    None means the manifest doesn't exist yet (fresh deploy) or is
    malformed — both treated as "device hasn't seen anything yet, so any
    reported sha is stale". The caller falls into the advance branch.
    """
    try:
        body = s3mod.get_object(publish.CURRENT_MANIFEST_KEY)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in _MISSING_OBJECT_CODES:
            return None
        raise
    try:
        return Manifest.from_json(body)
    except (json.JSONDecodeError, ValueError, KeyError):
        return None


def _manifest_fields(manifest: Manifest) -> dict[str, Any]:
    """Subset of manifest fields the firmware needs to skip a stale fetch.

    Echoed in the ``/wake`` response so the firmware can feed them
    straight into ``downloadVerifyAndDraw`` without a follow-up
    ``GET current/manifest.json`` that would hit CloudFront's 60–300 s
    cache and return the pre-advance manifest.
    """
    return {
        "image_url": manifest.image_url,
        "image_sha256": manifest.image_sha256,
        "image_bytes": manifest.image_bytes,
        "next_check_after": manifest.next_check_after,
    }


def _fire_replenish() -> None:
    """Async-invoke the generator with ``render_one`` to backfill the buffer.

    Failure is logged and swallowed — the cron will eventually top the
    buffer back up. We never want a flaky lambda:Invoke to fail the
    wake call after we already advanced current.
    """
    fn_name = os.environ.get("EINKGEN_GENERATOR_FUNCTION_NAME")
    if not fn_name:
        log.error(
            "ERROR EINKGEN_GENERATOR_FUNCTION_NAME unset; cannot trigger render_one"
        )
        return
    try:
        _get_lambda_client().invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=json.dumps({"action": "render_one"}).encode("utf-8"),
        )
    except Exception:
        log.exception("ERROR failed to invoke generator for render_one")


def _handle_wake(event: dict[str, Any]) -> dict[str, Any]:
    """``POST /wake`` — sha-debounced advance from the generated buffer.

    See module docstring for the response branches. The handler is
    idempotent under racing wake calls: two concurrent invocations with
    the same reported sha will both either advance to the same head or
    both ``noop``, modulo the rare case where one wins the
    ``delete_object`` race and the other re-pops the next head. Both
    outcomes are acceptable for the single-device deployment we target.
    """
    auth_err = _authorize(event)
    if auth_err is not None:
        return auth_err

    raw_body, err = _decode_body(event)
    if err is not None:
        return err
    assert raw_body is not None

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

    reported_raw = body.get("current_sha256")
    if reported_raw is None or reported_raw == "":
        # First-boot firmware has nothing in NVS yet; treat as "unknown sha"
        # which will fail the equality check below and trigger the advance
        # branch on initial deploy.
        reported_sha: str | None = None
    elif isinstance(reported_raw, str) and SHA256_HEX_RE.match(reported_raw):
        reported_sha = reported_raw
    else:
        return _response(
            400,
            {"error": "bad_request", "detail": "current_sha256 must be hex-64"},
        )

    current_manifest = _read_current_manifest()
    manifest_sha = current_manifest.image_sha256 if current_manifest else None

    # Branch 1: device hasn't drawn the current manifest yet. Tell it to
    # redraw the existing manifest rather than popping more off the buffer
    # — this is what makes rapid wake presses a no-op until the device
    # actually shows the previous pop. We embed the manifest fields so
    # the firmware can skip the follow-up GET (CloudFront caches
    # current/manifest.json for 60–300 s and an in-flight invalidation
    # typically takes 5–60 s to propagate).
    if current_manifest is not None and reported_sha != manifest_sha:
        return _response(
            200,
            {
                "action": "redraw",
                "manifest_sha256": manifest_sha,
                **_manifest_fields(current_manifest),
            },
        )

    # Branch 2: device is up-to-date (or fresh deploy with no manifest yet).
    # Advance to the head of the generated queue.
    head = generated_queue.peek_head()
    if head is None:
        return _response(
            200,
            {
                "action": "queue_empty",
                "manifest_sha256": manifest_sha or "",
            },
        )

    # Promote the head to current. Two concurrent wakes here can both
    # call set_current_from_history with the same id — that's fine, both
    # produce the same new manifest (modulo version increment) and the
    # device just sees the latest. The marker delete below is the only
    # racy step, and a doubled delete is a noop.
    try:
        new_manifest = publish.set_current_from_history(head.history_id)
    except publish.HistoryItemNotFound:
        # Stale marker — the history bytes were nuked somehow. Drop the
        # marker so the buffer can advance and report as if the queue
        # were empty for this call.
        log.error(
            "ERROR /wake: generated marker %s pointed at missing history",
            head.history_id,
        )
        try:
            generated_queue.finalize(head)
        except Exception:  # pragma: no cover - best-effort
            log.exception("failed to finalize stale generated marker")
        return _response(
            200,
            {"action": "queue_empty", "manifest_sha256": manifest_sha or ""},
        )

    try:
        generated_queue.finalize(head)
    except Exception:
        # Marker delete failed — log but don't fail the response. The
        # next wake will try again; in the worst case we just advance
        # twice to the same image and ``set_current_from_history`` is
        # idempotent.
        log.exception(
            "failed to finalize generated marker after advance (id=%s)",
            head.history_id,
        )

    _fire_replenish()

    return _response(
        200,
        {
            "action": "advance",
            "manifest_sha256": new_manifest.image_sha256,
            "history_id": head.history_id,
            **_manifest_fields(new_manifest),
        },
    )
