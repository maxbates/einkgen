"""HMAC-signed session cookies for the admin API.

Token wire format
-----------------
``<payload-b64url>.<sig-b64url>`` where:

- ``payload`` is a JSON object ``{"v":1,"sub":"admin","iat":...,"exp":...}``
  base64url-encoded **without padding**.
- ``sig`` is HMAC-SHA256 over the raw ``payload-b64url`` bytes, base64url
  -encoded without padding. Signing the encoded form (not the raw JSON) means
  verification doesn't need to canonicalise JSON whitespace.

Verification rejects:

- Malformed tokens (no dot, non-base64, non-JSON).
- Bad signatures (constant-time comparison via ``hmac.compare_digest``).
- Expired payloads (``exp`` <= now).
- Wrong schema version (``v != 1``) — gives us a no-downtime path for future
  format changes.

Cookies are emitted with ``HttpOnly; Secure; SameSite=Lax; Path=/admin``.
``SameSite=Lax`` is safe because the SPA and the admin API share an origin
(CloudFront routes ``/admin/*`` to the admin HTTP API), so the cookie isn't
"cross-site" in the browser's sense.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

SCHEMA_VERSION = 1
COOKIE_NAME = "einkgen_admin"
COOKIE_PATH = "/admin"
DEFAULT_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days


@dataclass(frozen=True)
class Payload:
    sub: str
    iat: int
    exp: int


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign(
    key: str,
    *,
    sub: str = "admin",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint a session token signed with ``key``."""
    issued = int(now if now is not None else time.time())
    payload = {
        "v": SCHEMA_VERSION,
        "sub": sub,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }
    payload_b64 = _b64u_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    sig = hmac.new(
        key.encode("utf-8"), payload_b64.encode("ascii"), sha256
    ).digest()
    return f"{payload_b64}.{_b64u_encode(sig)}"


def verify(token: str, key: str, *, now: int | None = None) -> Payload | None:
    """Return the decoded ``Payload`` if ``token`` is valid, else ``None``."""
    if not isinstance(token, str) or "." not in token:
        return None
    payload_b64, sig_b64 = token.rsplit(".", 1)
    try:
        provided_sig = _b64u_decode(sig_b64)
        payload_bytes = _b64u_decode(payload_b64)
    except (ValueError, base64.binascii.Error):
        return None
    expected_sig = hmac.new(
        key.encode("utf-8"), payload_b64.encode("ascii"), sha256
    ).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None
    try:
        payload: Any = json.loads(payload_bytes)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != SCHEMA_VERSION:
        return None
    sub = payload.get("sub")
    iat = payload.get("iat")
    exp = payload.get("exp")
    if not isinstance(sub, str) or not isinstance(iat, int) or not isinstance(exp, int):
        return None
    current = int(now if now is not None else time.time())
    if exp <= current:
        return None
    return Payload(sub=sub, iat=iat, exp=exp)


def build_set_cookie(token: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Build the ``Set-Cookie`` value for a freshly minted token."""
    return (
        f"{COOKIE_NAME}={token}; "
        f"Max-Age={ttl_seconds}; "
        f"Path={COOKIE_PATH}; "
        "HttpOnly; "
        "Secure; "
        "SameSite=Lax"
    )


def build_clear_cookie() -> str:
    """Build the ``Set-Cookie`` value that asks the browser to drop the cookie."""
    return (
        f"{COOKIE_NAME}=; "
        "Max-Age=0; "
        f"Path={COOKIE_PATH}; "
        "HttpOnly; "
        "Secure; "
        "SameSite=Lax"
    )


def parse_cookie_header(value: str | None) -> str | None:
    """Extract the ``einkgen_admin`` cookie value from a Cookie header.

    Browsers (and API Gateway) collapse multiple cookies into one header
    separated by ``; ``. We do a permissive split so trailing/leading
    whitespace and tab-separated variants still parse.
    """
    if not value:
        return None
    for pair in value.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, val = pair.partition("=")
        if name.strip() == COOKIE_NAME:
            return val.strip()
    return None
