"""Round-trip tests for the HMAC-signed admin cookie helper."""

from __future__ import annotations

import json

from einkgen.core import admin_cookie

KEY = "test-signing-key"
OTHER_KEY = "different-signing-key"


def test_sign_then_verify_round_trips():
    token = admin_cookie.sign(KEY, now=1_000_000)
    payload = admin_cookie.verify(token, KEY, now=1_000_000 + 1)
    assert payload is not None
    assert payload.sub == "admin"
    assert payload.iat == 1_000_000
    assert payload.exp == 1_000_000 + admin_cookie.DEFAULT_TTL_SECONDS


def test_verify_rejects_wrong_key():
    token = admin_cookie.sign(KEY, now=1_000_000)
    assert admin_cookie.verify(token, OTHER_KEY, now=1_000_001) is None


def test_verify_rejects_expired_token():
    token = admin_cookie.sign(KEY, ttl_seconds=60, now=1_000_000)
    assert admin_cookie.verify(token, KEY, now=1_000_000 + 61) is None
    # exp==now is also expired (strictly less-than required)
    assert (
        admin_cookie.verify(token, KEY, now=1_000_000 + 60) is None
    )


def test_verify_rejects_tampered_payload():
    token = admin_cookie.sign(KEY, now=1_000_000)
    payload_b64, sig_b64 = token.rsplit(".", 1)
    # Flip the last char of the payload — signature should no longer match.
    tampered_payload = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    tampered = f"{tampered_payload}.{sig_b64}"
    assert admin_cookie.verify(tampered, KEY, now=1_000_001) is None


def test_verify_rejects_malformed_tokens():
    assert admin_cookie.verify("", KEY) is None
    assert admin_cookie.verify("not-a-token", KEY) is None
    assert admin_cookie.verify("only.one.dot.too.many", KEY) is None
    assert admin_cookie.verify(".", KEY) is None
    # Wrong schema version is rejected too — gives us a clean migration path.
    fake = admin_cookie._b64u_encode(
        json.dumps({"v": 999, "sub": "admin", "iat": 1, "exp": 9_999_999_999}).encode()
    )
    assert admin_cookie.verify(f"{fake}.AAAA", KEY) is None


def test_parse_cookie_header_extracts_named_cookie():
    assert (
        admin_cookie.parse_cookie_header("einkgen_admin=abc.def; other=1")
        == "abc.def"
    )
    assert (
        admin_cookie.parse_cookie_header("other=1; einkgen_admin=xyz")
        == "xyz"
    )
    assert admin_cookie.parse_cookie_header("other=1") is None
    assert admin_cookie.parse_cookie_header("") is None
    assert admin_cookie.parse_cookie_header(None) is None


def test_build_set_cookie_includes_security_attributes():
    set_cookie = admin_cookie.build_set_cookie("token-here", ttl_seconds=3600)
    assert set_cookie.startswith("einkgen_admin=token-here;")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "Max-Age=3600" in set_cookie


def test_build_clear_cookie_zero_max_age():
    clear = admin_cookie.build_clear_cookie()
    assert "Max-Age=0" in clear
    assert "einkgen_admin=;" in clear
