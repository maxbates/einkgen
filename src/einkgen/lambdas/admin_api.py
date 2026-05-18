"""``einkgen-admin-api`` Lambda — operator-only write access for the SPA.

Routes (all under the ``/admin`` prefix, behind a single HTTP API):

- ``POST /admin/login``         → ``{"password": "..."}``  → 204 + ``Set-Cookie``
- ``GET  /admin/me``            → 200 ``{"authenticated": true}`` or 401
- ``POST /admin/logout``        → 204 + cookie-clear ``Set-Cookie``
- ``POST /admin/queue/prompt``  → ``{"prompt":..., "at":"top|bottom|now"}``
                                                            → 200 ``{"id":...}``
- ``POST /admin/queue/image``   → ``{"filename":..., "image_b64":..., "prompt":?, "at":?}``
                                                            → 200 ``{"id":...}``
- ``POST /admin/queue/<id>/run``→ 202 (async-invoke generator to render this specific item, regardless of position)
- ``DELETE /admin/queue/<id>``  → 204 (cancel a pending item)
- ``GET  /admin/prompts``       → 200 ``{"prompts": [...], "is_default": bool}``
- ``PUT  /admin/prompts``       → ``{"prompts": [...]}``    → 200 ``{"prompts": [...]}``
- ``POST /admin/prompts/reset`` → 200 ``{"prompts": [...]}`` (writes DEFAULTS)
- ``POST /admin/show``          → ``{"history_id": "..."}`` → 200 ``{"version":...}``
- ``GET  /admin/failures``      → 200 ``{"items": [...]}`` (last-hour drops)

The ``at`` field on enqueue routes controls which of the two priority
queues the item lands in: ``"bottom"`` (default) appends to the bottom
queue; ``"top"`` appends to the top queue (which always drains before
the bottom one); ``"now"`` writes to the top queue **and** async-invokes
the generator so the new item is rendered immediately.

``/run`` is the equivalent shortcut for an item that's already on the
queue — it async-invokes the generator with ``render_item`` so that
specific item renders next, without any reordering or in-place rewrite
of S3 objects.

Auth
----
A successful ``/admin/login`` mints an HMAC-signed cookie (see
``einkgen.core.admin_cookie``) valid for 90 days. Every subsequent route
requires that cookie. We deliberately do not implement password lockout or
rate-limiting at the application layer — the front door is API Gateway +
CloudFront, the password lives in Secrets Manager, and the threat model is
"one operator, one password, low value of compromise".

Cookies are pinned to ``Path=/admin`` so they never get sent to the public
read API on the same origin.

Image uploads
-------------
Images are sent base64-encoded inside JSON. We could parse multipart, but
JSON keeps this Lambda dependency-free and matches the CLI shape (``einkgen
queue image <path>``). API Gateway HTTP API caps payloads at 10 MB, so the
effective image limit is ~7 MB after base64 expansion — comfortable for
phone photos.

Environment
-----------
- ``EINKGEN_BUCKET``                — queue/staged uploads land here.
- ``ADMIN_PASSWORD_SECRET_NAME``    — default ``einkgen/admin_password``.
- ``ADMIN_COOKIE_KEY_SECRET_NAME``  — default ``einkgen/admin_cookie_signing_key``.
- ``EINKGEN_GENERATOR_FUNCTION_NAME`` — name of the generator Lambda;
  required for ``at="now"`` and ``/run``. Without it those routes 500.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any

import boto3

from einkgen.core import admin_cookie, failures, prompt_library, publish, queue
from einkgen.core import s3 as s3mod

log = logging.getLogger(__name__)

DEFAULT_PASSWORD_SECRET = "einkgen/admin_password"
DEFAULT_COOKIE_KEY_SECRET = "einkgen/admin_cookie_signing_key"

# Same cache TTL as device-status — bounds how long a rotated password stays
# unreachable to warm Lambdas.
SECRET_CACHE_TTL_SECONDS = 300

# Upload caps. The hard ceiling is API Gateway HTTP API's 10 MB payload limit;
# we apply a smaller cap on the decoded image so a single oversized upload
# can't blow the request budget on a tight Lambda timeout.
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # 10 MB JSON body cap
MAX_IMAGE_BYTES = 8 * 1024 * 1024     # 8 MB decoded image
MAX_PROMPT_CHARS = 4000               # plenty for any sane restyle hint
MAX_LIBRARY_ENTRIES = 200             # ample headroom over the seed 10

# Same charset cap the CLI uses when staging a local image.
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# History ids are ULIDs (26-char Crockford base32). We accept anything that
# matches the ULID-shaped alphabet to avoid a tight format coupling, but
# refuse path separators / slashes / dots so the id can never escape the
# ``history/<id>/manifest.json`` key the publish helper constructs.
HISTORY_ID_RE = re.compile(r"^[A-Z0-9]{8,32}$")

# Queue ids share the same ULID-shaped alphabet — they're issued by the
# same generator. Use the same regex so /admin/queue/<id>/... can't be
# tricked into walking S3 paths via slashes or dots in the id segment.
QUEUE_ID_RE = HISTORY_ID_RE

# Placement options accepted on enqueue routes.
ALLOWED_AT = ("top", "bottom", "now")
DEFAULT_AT = "bottom"

_BASE_HEADERS = {
    "Content-Type": "application/json",
    # Cache-Control on every admin response so a CDN doesn't accidentally
    # cache a 401 (which would lock out a freshly-logged-in browser).
    "Cache-Control": "no-store",
}

_cached_password: str | None = None
_cached_password_at: float = 0.0
_cached_cookie_key: str | None = None
_cached_cookie_key_at: float = 0.0
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
    """Drop cached secrets + client. Used by tests."""
    global _cached_password, _cached_password_at
    global _cached_cookie_key, _cached_cookie_key_at, _sm_client, _lambda_client
    _cached_password = None
    _cached_password_at = 0.0
    _cached_cookie_key = None
    _cached_cookie_key_at = 0.0
    _sm_client = None
    _lambda_client = None


def _read_secret_string(secret_name: str) -> str:
    resp = _get_sm_client().get_secret_value(SecretId=secret_name)
    if "SecretString" not in resp:
        raise RuntimeError(
            f"secret {secret_name!r} has no SecretString; "
            "use --secret-string (not --secret-binary) when rotating."
        )
    return resp["SecretString"]


def _expected_password() -> str:
    global _cached_password, _cached_password_at
    now = time.monotonic()
    if _cached_password is not None and (now - _cached_password_at) < SECRET_CACHE_TTL_SECONDS:
        return _cached_password
    secret_name = os.environ.get("ADMIN_PASSWORD_SECRET_NAME", DEFAULT_PASSWORD_SECRET)
    _cached_password = _read_secret_string(secret_name)
    _cached_password_at = now
    return _cached_password


def _cookie_key() -> str:
    global _cached_cookie_key, _cached_cookie_key_at
    now = time.monotonic()
    if _cached_cookie_key is not None and (now - _cached_cookie_key_at) < SECRET_CACHE_TTL_SECONDS:
        return _cached_cookie_key
    secret_name = os.environ.get(
        "ADMIN_COOKIE_KEY_SECRET_NAME", DEFAULT_COOKIE_KEY_SECRET
    )
    _cached_cookie_key = _read_secret_string(secret_name)
    _cached_cookie_key_at = now
    return _cached_cookie_key


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _response(
    status: int,
    body: Any = None,
    *,
    extra_headers: dict[str, str] | None = None,
    cookies: list[str] | None = None,
) -> dict[str, Any]:
    """Build a Lambda proxy v2 response.

    ``cookies`` is the HTTP API v2 mechanism for ``Set-Cookie`` — the platform
    serialises each entry as its own header (multi-valued headers in the
    ``headers`` dict are concatenated, which corrupts cookie syntax).
    """
    headers = dict(_BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    resp: dict[str, Any] = {
        "statusCode": status,
        "headers": headers,
    }
    if body is not None:
        resp["body"] = json.dumps(body)
    if cookies:
        resp["cookies"] = cookies
    return resp


def _method(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    return str(method).upper()


def _path(event: dict[str, Any]) -> str:
    raw = event.get("rawPath")
    if isinstance(raw, str) and raw:
        path = raw
    else:
        ctx = event.get("requestContext") or {}
        http = ctx.get("http") or {}
        path = http.get("path", "/")
    if not isinstance(path, str) or not path:
        return "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _parse_json_body(event: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(body, error_response)``."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as exc:
            return None, _response(
                400, {"error": "bad_request", "detail": f"invalid base64 body: {exc}"}
            )
    if not raw:
        return None, _response(400, {"error": "bad_request", "detail": "empty body"})
    if len(raw) > MAX_REQUEST_BYTES:
        return None, _response(
            413, {"error": "payload_too_large", "detail": "request body too large"}
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, _response(
            400, {"error": "bad_request", "detail": f"invalid json: {exc.msg}"}
        )
    if not isinstance(parsed, dict):
        return None, _response(
            400, {"error": "bad_request", "detail": "body must be a JSON object"}
        )
    return parsed, None


def _cookie_value(event: dict[str, Any]) -> str | None:
    """Extract the ``einkgen_admin`` cookie from either v2's ``cookies`` list or a Cookie header."""
    cookies = event.get("cookies")
    if isinstance(cookies, list):
        for raw in cookies:
            val = admin_cookie.parse_cookie_header(raw)
            if val:
                return val
    headers = event.get("headers") or {}
    for key, val in headers.items():
        if key.lower() == "cookie":
            extracted = admin_cookie.parse_cookie_header(val)
            if extracted:
                return extracted
    return None


def _require_session(event: dict[str, Any]) -> admin_cookie.Payload | None:
    raw = _cookie_value(event)
    if not raw:
        return None
    try:
        key = _cookie_key()
    except Exception:
        log.exception("ERROR failed to read admin cookie signing key")
        return None
    return admin_cookie.verify(raw, key)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _handle_login(event: dict[str, Any]) -> dict[str, Any]:
    body, err = _parse_json_body(event)
    if err is not None:
        return err
    assert body is not None
    provided = body.get("password")
    if not isinstance(provided, str) or not provided:
        return _response(400, {"error": "bad_request", "detail": "missing password"})
    try:
        expected = _expected_password()
    except Exception:
        log.exception("ERROR failed to fetch admin password secret")
        # Don't leak "service degraded" vs "wrong password" — caller sees 401 either way.
        return _response(401, {"error": "unauthorized"})
    if expected == "REPLACE_ME_POST_DEPLOY":
        # Placeholder still in place — refuse to authenticate so a fresh deploy
        # isn't accidentally world-writable until the operator runs §3.5.
        log.error("ERROR admin_password secret still holds the placeholder value")
        return _response(503, {"error": "not_configured"})
    if not hmac.compare_digest(provided, expected):
        return _response(401, {"error": "unauthorized"})
    try:
        key = _cookie_key()
    except Exception:
        log.exception("ERROR failed to fetch admin cookie signing key")
        return _response(500, {"error": "internal"})
    token = admin_cookie.sign(key)
    return _response(204, cookies=[admin_cookie.build_set_cookie(token)])


def _handle_me(event: dict[str, Any]) -> dict[str, Any]:
    payload = _require_session(event)
    if payload is None:
        return _response(401, {"authenticated": False})
    return _response(200, {"authenticated": True, "sub": payload.sub, "exp": payload.exp})


def _handle_logout() -> dict[str, Any]:
    return _response(204, cookies=[admin_cookie.build_clear_cookie()])


def _parse_at(body: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Validate the optional ``at`` field. Returns ``(at, error_response)``."""
    raw = body.get("at", DEFAULT_AT)
    if not isinstance(raw, str) or raw not in ALLOWED_AT:
        return DEFAULT_AT, _response(
            400,
            {
                "error": "bad_request",
                "detail": f"at must be one of {ALLOWED_AT!r}",
            },
        )
    return raw, None


def _enqueue_placement(at: str) -> str:
    """``at="now"`` enqueues at the top; the async render is fired separately."""
    return "top" if at == "now" else at


def _invoke_generator(payload: dict[str, Any]) -> bool:
    """Fire-and-forget invoke of the generator Lambda.

    Returns whether the invoke succeeded. Failures are logged and
    swallowed by the caller — the item is on the queue regardless, so
    the worst case is "wait for the next cron tick" rather than data
    loss.
    """
    fn_name = os.environ.get("EINKGEN_GENERATOR_FUNCTION_NAME")
    if not fn_name:
        log.error(
            "ERROR EINKGEN_GENERATOR_FUNCTION_NAME unset; cannot invoke generator"
        )
        return False
    try:
        _get_lambda_client().invoke(
            FunctionName=fn_name,
            InvocationType="Event",  # async — returns 202 immediately
            Payload=json.dumps(payload).encode("utf-8"),
        )
        return True
    except Exception:
        log.exception("ERROR failed to invoke generator with payload=%r", payload)
        return False


def _trigger_render_now() -> bool:
    """Async-invoke the generator to render the current head item."""
    return _invoke_generator({"action": "render_now"})


def _trigger_render_item(item_id: str) -> bool:
    """Async-invoke the generator to render a specific pending item."""
    return _invoke_generator({"action": "render_item", "item_id": item_id})


def _handle_queue_prompt(event: dict[str, Any]) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    body, err = _parse_json_body(event)
    if err is not None:
        return err
    assert body is not None
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _response(400, {"error": "bad_request", "detail": "missing prompt"})
    if len(prompt) > MAX_PROMPT_CHARS:
        return _response(
            413, {"error": "payload_too_large", "detail": "prompt too long"}
        )
    at, at_err = _parse_at(body)
    if at_err is not None:
        return at_err
    item = queue.enqueue(
        "prompt",
        prompt=prompt.strip(),
        source="admin",
        at=_enqueue_placement(at),
    )
    if at == "now":
        _trigger_render_now()
    return _response(
        200,
        {"id": item.id, "kind": item.kind, "at": at},
    )


def _safe_filename(name: str) -> str:
    base = os.path.basename(name) or "image"
    sanitized = SAFE_FILENAME_RE.sub("_", base)
    return sanitized[:80] or "image"


def _handle_queue_image(event: dict[str, Any]) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    body, err = _parse_json_body(event)
    if err is not None:
        return err
    assert body is not None
    image_b64 = body.get("image_b64")
    if not isinstance(image_b64, str) or not image_b64:
        return _response(400, {"error": "bad_request", "detail": "missing image_b64"})
    try:
        data = base64.b64decode(image_b64, validate=True)
    except Exception as exc:
        return _response(
            400, {"error": "bad_request", "detail": f"invalid base64 image: {exc}"}
        )
    if len(data) > MAX_IMAGE_BYTES:
        return _response(
            413, {"error": "payload_too_large", "detail": "image too large"}
        )
    if not data:
        return _response(400, {"error": "bad_request", "detail": "empty image"})
    filename_raw = body.get("filename")
    filename = _safe_filename(filename_raw if isinstance(filename_raw, str) else "image")
    prompt_raw = body.get("prompt")
    prompt: str | None = None
    if isinstance(prompt_raw, str) and prompt_raw.strip():
        if len(prompt_raw) > MAX_PROMPT_CHARS:
            return _response(
                413, {"error": "payload_too_large", "detail": "prompt too long"}
            )
        prompt = prompt_raw.strip()
    at, at_err = _parse_at(body)
    if at_err is not None:
        return at_err
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    staged_key = f"{queue.STAGED_PREFIX}{sha8}-{filename}"
    s3mod.put_object(staged_key, data)
    item = queue.enqueue(
        "image",
        image_s3_key=staged_key,
        prompt=prompt,
        source="admin",
        at=_enqueue_placement(at),
    )
    if at == "now":
        _trigger_render_now()
    return _response(
        200,
        {"id": item.id, "kind": item.kind, "at": at},
    )


def _handle_queue_run(event: dict[str, Any], item_id: str) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    # Check existence before invoking — saves a Lambda spin-up and gives
    # the operator a clean 404 on "this just got drained" instead of
    # silently no-op-ing inside the generator.
    item = queue.get(item_id)
    if item is None:
        return _response(404, {"error": "not_found", "detail": "no such queue item"})
    fired = _trigger_render_item(item_id)
    return _response(
        202 if fired else 502,
        {
            "id": item.id,
            "kind": item.kind,
            "render_triggered": fired,
        },
    )


def _handle_queue_delete(event: dict[str, Any], item_id: str) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    if not queue.cancel(item_id):
        return _response(404, {"error": "not_found", "detail": "no such queue item"})
    return _response(204)


def _handle_show(event: dict[str, Any]) -> dict[str, Any]:
    """Re-publish an existing history item as the current frame.

    No copy, no regenerate — just point the manifest at the history bmp.
    See ``einkgen.core.publish.set_current_from_history``.
    """
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    body, err = _parse_json_body(event)
    if err is not None:
        return err
    assert body is not None
    history_id = body.get("history_id")
    if not isinstance(history_id, str) or not HISTORY_ID_RE.match(history_id):
        return _response(
            400, {"error": "bad_request", "detail": "missing or malformed history_id"}
        )
    try:
        manifest = publish.set_current_from_history(history_id)
    except publish.HistoryItemNotFound:
        return _response(404, {"error": "not_found", "detail": "no such history item"})
    return _response(
        200,
        {
            "version": manifest.version,
            "image_sha256": manifest.image_sha256,
            "history_id": history_id,
        },
    )


# ---------------------------------------------------------------------------
# /admin/prompts — operator-editable random-pick library
# ---------------------------------------------------------------------------


def _handle_prompts_get(event: dict[str, Any]) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    current = list(prompt_library.load(force=True))
    return _response(
        200,
        {
            "prompts": current,
            "is_default": tuple(current) == prompt_library.DEFAULTS,
            "defaults": list(prompt_library.DEFAULTS),
        },
    )


def _handle_prompts_put(event: dict[str, Any]) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    body, err = _parse_json_body(event)
    if err is not None:
        return err
    assert body is not None
    raw = body.get("prompts")
    if not isinstance(raw, list):
        return _response(
            400, {"error": "bad_request", "detail": "prompts must be a list"}
        )
    if len(raw) > MAX_LIBRARY_ENTRIES:
        return _response(
            413,
            {
                "error": "payload_too_large",
                "detail": f"too many entries (max {MAX_LIBRARY_ENTRIES})",
            },
        )
    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            return _response(
                400, {"error": "bad_request", "detail": "prompts must be strings"}
            )
        if len(entry) > MAX_PROMPT_CHARS:
            return _response(
                413,
                {"error": "payload_too_large", "detail": "individual prompt too long"},
            )
        cleaned.append(entry)
    try:
        persisted = prompt_library.write(cleaned)
    except ValueError as exc:
        return _response(400, {"error": "bad_request", "detail": str(exc)})
    return _response(
        200,
        {
            "prompts": list(persisted),
            "is_default": persisted == prompt_library.DEFAULTS,
        },
    )


def _handle_prompts_reset(event: dict[str, Any]) -> dict[str, Any]:
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    persisted = prompt_library.reset_to_defaults()
    return _response(
        200,
        {"prompts": list(persisted), "is_default": True},
    )


def _handle_failures_get(event: dict[str, Any]) -> dict[str, Any]:
    """Last-hour breadcrumbs for permanently-dropped items.

    Returns the same fields the SPA needs to render a "Recently rejected"
    panel: id, timestamps, source, kind, the (truncated) prompt, and the
    reason the pipeline gave up. See ``einkgen.core.failures``.
    """
    if _require_session(event) is None:
        return _response(401, {"error": "unauthorized"})
    items = [rec.to_json() for rec in failures.list_recent()]
    return _response(200, {"items": items})


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_ROUTES: dict[tuple[str, str], Any] = {
    ("POST", "/admin/login"): _handle_login,
    ("GET", "/admin/me"): _handle_me,
    ("POST", "/admin/logout"): None,  # filled in below — no event needed
    ("POST", "/admin/queue/prompt"): _handle_queue_prompt,
    ("POST", "/admin/queue/image"): _handle_queue_image,
    ("GET", "/admin/prompts"): _handle_prompts_get,
    ("PUT", "/admin/prompts"): _handle_prompts_put,
    ("POST", "/admin/prompts/reset"): _handle_prompts_reset,
    ("POST", "/admin/show"): _handle_show,
    ("GET", "/admin/failures"): _handle_failures_get,
}


# Per-item queue routes — ``/admin/queue/<id>/run`` plus
# ``DELETE /admin/queue/<id>``. Matched separately from _ROUTES because
# they need to extract <id> from the path and validate it before
# touching S3.
_PER_ITEM_ROUTE_RE = re.compile(
    r"^/admin/queue/(?P<id>[^/]+)(?:/(?P<verb>run))?$"
)


def _dispatch_per_item(method: str, path: str, event: dict[str, Any]) -> dict[str, Any] | None:
    m = _PER_ITEM_ROUTE_RE.match(path)
    if m is None:
        return None
    item_id = m.group("id")
    verb = m.group("verb")
    # Reject ids that don't look ULID-shaped before any auth check so a
    # malformed path never reaches S3-key construction.
    if not QUEUE_ID_RE.match(item_id):
        return _response(400, {"error": "bad_request", "detail": "malformed queue id"})
    if verb == "run" and method == "POST":
        return _handle_queue_run(event, item_id)
    if verb is None and method == "DELETE":
        return _handle_queue_delete(event, item_id)
    return None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    try:
        method = _method(event)
        path = _path(event)
        if (method, path) == ("POST", "/admin/logout"):
            return _handle_logout()
        route = _ROUTES.get((method, path))
        if route is not None:
            return route(event)
        per_item = _dispatch_per_item(method, path, event)
        if per_item is not None:
            return per_item
        return _response(404, {"error": "not_found"})
    except Exception:
        log.exception("ERROR admin_api unhandled error")
        return _response(500, {"error": "internal"})
