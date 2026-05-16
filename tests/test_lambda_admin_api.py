"""Tests for the admin-api Lambda."""

from __future__ import annotations

import base64
import json

import boto3
import pytest

from datetime import datetime, timezone

from einkgen.core import admin_cookie, prompt_library, publish, queue
from einkgen.lambdas import admin_api

TEST_PASSWORD = "photos-for-anyone"
TEST_COOKIE_KEY = "very-secret-hmac-key"
PASSWORD_SECRET_NAME = "einkgen-test/admin_password"
COOKIE_SECRET_NAME = "einkgen-test/admin_cookie_signing_key"


@pytest.fixture
def reset_cache():
    admin_api._reset_cache()
    prompt_library._reset_cache()
    yield
    admin_api._reset_cache()
    prompt_library._reset_cache()


@pytest.fixture
def secrets(s3_bucket, monkeypatch, reset_cache):
    monkeypatch.setenv("ADMIN_PASSWORD_SECRET_NAME", PASSWORD_SECRET_NAME)
    monkeypatch.setenv("ADMIN_COOKIE_KEY_SECRET_NAME", COOKIE_SECRET_NAME)
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(Name=PASSWORD_SECRET_NAME, SecretString=TEST_PASSWORD)
    sm.create_secret(Name=COOKIE_SECRET_NAME, SecretString=TEST_COOKIE_KEY)
    return sm


def _event(
    method: str,
    path: str,
    *,
    body: dict | str | None = None,
    cookies: list[str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    raw_body: str | None
    if isinstance(body, dict):
        raw_body = json.dumps(body)
    else:
        raw_body = body
    event: dict = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": dict(headers or {}),
        "isBase64Encoded": False,
    }
    if raw_body is not None:
        event["body"] = raw_body
    if cookies:
        event["cookies"] = cookies
    return event


def _cookie_header_from_response(resp: dict) -> str:
    cookies = resp.get("cookies") or []
    assert cookies, "expected Set-Cookie in response"
    # First (and only) cookie line — strip everything after the first ';'
    return cookies[0]


def _session_cookie_value(resp: dict) -> str:
    header = _cookie_header_from_response(resp)
    value = admin_cookie.parse_cookie_header(header)
    assert value, f"could not extract cookie from {header!r}"
    return value


def _login(password: str = TEST_PASSWORD) -> dict:
    return admin_api.handler(
        _event("POST", "/admin/login", body={"password": password})
    )


# ---------------------------------------------------------------------------
# /admin/login
# ---------------------------------------------------------------------------


def test_login_correct_password_returns_cookie(secrets):
    resp = _login()
    assert resp["statusCode"] == 204
    set_cookie = _cookie_header_from_response(resp)
    assert "HttpOnly" in set_cookie and "Secure" in set_cookie
    # And the embedded token actually round-trips through verify.
    token = _session_cookie_value(resp)
    payload = admin_cookie.verify(token, TEST_COOKIE_KEY)
    assert payload is not None and payload.sub == "admin"


def test_login_wrong_password_returns_401(secrets):
    resp = admin_api.handler(
        _event("POST", "/admin/login", body={"password": "nope"})
    )
    assert resp["statusCode"] == 401
    assert "cookies" not in resp


def test_login_missing_password_returns_400(secrets):
    resp = admin_api.handler(_event("POST", "/admin/login", body={}))
    assert resp["statusCode"] == 400


def test_login_empty_body_returns_400(secrets):
    resp = admin_api.handler(_event("POST", "/admin/login", body=""))
    assert resp["statusCode"] == 400


def test_login_invalid_json_returns_400(secrets):
    resp = admin_api.handler(_event("POST", "/admin/login", body="{not json"))
    assert resp["statusCode"] == 400


def test_login_with_placeholder_password_returns_503(monkeypatch, s3_bucket, reset_cache):
    """First-deploy guardrail: refuse to authenticate against the CDK placeholder."""
    monkeypatch.setenv("ADMIN_PASSWORD_SECRET_NAME", PASSWORD_SECRET_NAME)
    monkeypatch.setenv("ADMIN_COOKIE_KEY_SECRET_NAME", COOKIE_SECRET_NAME)
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(
        Name=PASSWORD_SECRET_NAME, SecretString="REPLACE_ME_POST_DEPLOY"
    )
    sm.create_secret(Name=COOKIE_SECRET_NAME, SecretString=TEST_COOKIE_KEY)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/login",
            body={"password": "REPLACE_ME_POST_DEPLOY"},
        )
    )
    assert resp["statusCode"] == 503
    assert json.loads(resp["body"])["error"] == "not_configured"


# ---------------------------------------------------------------------------
# /admin/me
# ---------------------------------------------------------------------------


def test_me_without_cookie_returns_401(secrets):
    resp = admin_api.handler(_event("GET", "/admin/me"))
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["authenticated"] is False


def test_me_with_valid_cookie_returns_200(secrets):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event("GET", "/admin/me", cookies=[f"einkgen_admin={token}"])
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["authenticated"] is True
    assert body["sub"] == "admin"


def test_me_accepts_cookie_header_form(secrets):
    """API Gateway sometimes forwards Cookie as a header instead of cookies[]."""
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "GET",
            "/admin/me",
            headers={"cookie": f"einkgen_admin={token}; other=1"},
        )
    )
    assert resp["statusCode"] == 200


def test_me_with_tampered_cookie_returns_401(secrets):
    login = _login()
    token = _session_cookie_value(login)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    resp = admin_api.handler(
        _event("GET", "/admin/me", cookies=[f"einkgen_admin={tampered}"])
    )
    assert resp["statusCode"] == 401


# ---------------------------------------------------------------------------
# /admin/logout
# ---------------------------------------------------------------------------


def test_logout_returns_clear_cookie(secrets):
    resp = admin_api.handler(_event("POST", "/admin/logout"))
    assert resp["statusCode"] == 204
    set_cookie = _cookie_header_from_response(resp)
    assert "Max-Age=0" in set_cookie


# ---------------------------------------------------------------------------
# /admin/queue/prompt
# ---------------------------------------------------------------------------


def test_enqueue_prompt_requires_session(secrets, s3_bucket):
    resp = admin_api.handler(
        _event("POST", "/admin/queue/prompt", body={"prompt": "hi"})
    )
    assert resp["statusCode"] == 401
    # And nothing landed on S3.
    assert queue.list() == []


def test_enqueue_prompt_happy_path(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/queue/prompt",
            body={"prompt": "  Bold geometric shapes.  "},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["kind"] == "prompt"
    # The item is actually in S3, and the source is tagged "admin".
    items = queue.list()
    assert len(items) == 1
    only = items[0]
    assert only.id == body["id"]
    assert only.prompt == "Bold geometric shapes."
    assert only.source == "admin"
    assert only.kind == "prompt"


def test_enqueue_prompt_blank_returns_400(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/queue/prompt",
            body={"prompt": "   "},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 400
    assert queue.list() == []


# ---------------------------------------------------------------------------
# /admin/queue/image
# ---------------------------------------------------------------------------


def test_enqueue_image_requires_session(secrets, s3_bucket):
    payload = {
        "filename": "x.jpg",
        "image_b64": base64.b64encode(b"hello").decode(),
    }
    resp = admin_api.handler(_event("POST", "/admin/queue/image", body=payload))
    assert resp["statusCode"] == 401
    assert queue.list() == []


def test_enqueue_image_happy_path(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    image_bytes = b"\x89PNG\r\n\x1a\nFAKEPNG"
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/queue/image",
            body={
                "filename": "photo.png",
                "image_b64": base64.b64encode(image_bytes).decode(),
                "prompt": " restyle as woodcut ",
            },
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["kind"] == "image"
    items = queue.list()
    assert len(items) == 1
    only = items[0]
    assert only.id == body["id"]
    assert only.kind == "image"
    assert only.prompt == "restyle as woodcut"
    assert only.image_s3_key is not None
    assert only.image_s3_key.startswith(queue.STAGED_PREFIX)
    # The staged object exists with the original bytes.
    staged = s3_bucket.get_object(Bucket="einkgen-test", Key=only.image_s3_key)[
        "Body"
    ].read()
    assert staged == image_bytes


def test_enqueue_image_filename_is_sanitized(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/queue/image",
            body={
                "filename": "../weird name? .png",
                "image_b64": base64.b64encode(b"data").decode(),
            },
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 200
    items = queue.list()
    assert items[0].image_s3_key is not None
    # The dangerous path component is stripped; the staged key never contains '..'
    assert ".." not in items[0].image_s3_key
    assert " " not in items[0].image_s3_key


def test_enqueue_image_bad_base64_returns_400(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/queue/image",
            body={"filename": "x.jpg", "image_b64": "@@@not base64@@@"},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# /admin/prompts
# ---------------------------------------------------------------------------


def _auth_cookies() -> list[str]:
    login = _login()
    token = _session_cookie_value(login)
    return [f"einkgen_admin={token}"]


def test_get_prompts_requires_session(secrets, s3_bucket):
    resp = admin_api.handler(_event("GET", "/admin/prompts"))
    assert resp["statusCode"] == 401


def test_get_prompts_returns_defaults_when_unconfigured(secrets, s3_bucket):
    resp = admin_api.handler(
        _event("GET", "/admin/prompts", cookies=_auth_cookies())
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["prompts"] == list(prompt_library.DEFAULTS)
    assert body["is_default"] is True
    assert body["defaults"] == list(prompt_library.DEFAULTS)


def test_get_prompts_reflects_saved_state(secrets, s3_bucket):
    prompt_library.write(["custom one", "custom two"])
    resp = admin_api.handler(
        _event("GET", "/admin/prompts", cookies=_auth_cookies())
    )
    body = json.loads(resp["body"])
    assert body["prompts"] == ["custom one", "custom two"]
    assert body["is_default"] is False


def test_put_prompts_requires_session(secrets, s3_bucket):
    resp = admin_api.handler(
        _event("PUT", "/admin/prompts", body={"prompts": ["x"]})
    )
    assert resp["statusCode"] == 401
    # And nothing landed on S3.
    assert prompt_library.load(force=True) == prompt_library.DEFAULTS


def test_put_prompts_happy_path(secrets, s3_bucket):
    resp = admin_api.handler(
        _event(
            "PUT",
            "/admin/prompts",
            body={"prompts": ["  one ", "two", "one", "# skip"]},
            cookies=_auth_cookies(),
        )
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["prompts"] == ["one", "two"]
    assert body["is_default"] is False
    # And the file actually landed on S3.
    assert prompt_library.load(force=True) == ("one", "two")


def test_put_prompts_with_only_blanks_returns_400(secrets, s3_bucket):
    resp = admin_api.handler(
        _event(
            "PUT",
            "/admin/prompts",
            body={"prompts": ["  ", "# only comments"]},
            cookies=_auth_cookies(),
        )
    )
    assert resp["statusCode"] == 400


def test_put_prompts_non_list_returns_400(secrets, s3_bucket):
    resp = admin_api.handler(
        _event(
            "PUT",
            "/admin/prompts",
            body={"prompts": "one, two, three"},
            cookies=_auth_cookies(),
        )
    )
    assert resp["statusCode"] == 400


def test_put_prompts_non_string_entry_returns_400(secrets, s3_bucket):
    resp = admin_api.handler(
        _event(
            "PUT",
            "/admin/prompts",
            body={"prompts": ["ok", 42]},
            cookies=_auth_cookies(),
        )
    )
    assert resp["statusCode"] == 400


def test_put_prompts_overlong_entry_returns_413(secrets, s3_bucket):
    huge = "x" * (admin_api.MAX_PROMPT_CHARS + 1)
    resp = admin_api.handler(
        _event(
            "PUT",
            "/admin/prompts",
            body={"prompts": ["ok", huge]},
            cookies=_auth_cookies(),
        )
    )
    assert resp["statusCode"] == 413


def test_put_prompts_too_many_entries_returns_413(secrets, s3_bucket):
    too_many = [f"prompt {i}" for i in range(admin_api.MAX_LIBRARY_ENTRIES + 1)]
    resp = admin_api.handler(
        _event(
            "PUT",
            "/admin/prompts",
            body={"prompts": too_many},
            cookies=_auth_cookies(),
        )
    )
    assert resp["statusCode"] == 413


def test_post_prompts_reset_requires_session(secrets, s3_bucket):
    resp = admin_api.handler(_event("POST", "/admin/prompts/reset"))
    assert resp["statusCode"] == 401


def test_post_prompts_reset_restores_defaults(secrets, s3_bucket):
    prompt_library.write(["custom"])
    assert prompt_library.load(force=True) == ("custom",)
    resp = admin_api.handler(
        _event("POST", "/admin/prompts/reset", cookies=_auth_cookies())
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["prompts"] == list(prompt_library.DEFAULTS)
    assert body["is_default"] is True
    assert prompt_library.load(force=True) == prompt_library.DEFAULTS


# ---------------------------------------------------------------------------
# /admin/show
# ---------------------------------------------------------------------------


def _seed_history(item_id: str = "01HISTORYAA") -> None:
    publish.publish(
        b"BMP" + b"\x00" * 100,
        source={"kind": "generated", "model": "gpt-image-2", "prompt": "seeded"},
        item_id=item_id,
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )


def test_show_requires_session(secrets, s3_bucket):
    _seed_history()
    resp = admin_api.handler(
        _event("POST", "/admin/show", body={"history_id": "01HISTORYAA"})
    )
    assert resp["statusCode"] == 401


def test_show_happy_path_rewrites_manifest(secrets, s3_bucket):
    _seed_history("01HISTORYAA")
    # Generate a second history item so current/* points elsewhere.
    publish.publish(
        b"BMP" + b"\x00" * 50 + b"X",
        source={"kind": "generated", "prompt": "newer"},
        item_id="01HISTORYBB",
        now=datetime(2026, 5, 13, 15, 0, 0, tzinfo=timezone.utc),
    )

    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/show",
            body={"history_id": "01HISTORYAA"},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["history_id"] == "01HISTORYAA"
    assert body["version"] == 3

    # And the new current/manifest.json points at the older history bmp.
    live = s3_bucket.get_object(
        Bucket="einkgen-test", Key="current/manifest.json"
    )["Body"].read()
    data = json.loads(live)
    assert data["image_url"].endswith("/history/01HISTORYAA/processed.bmp")
    assert data["source"]["replayed_from"] == "01HISTORYAA"


def test_show_missing_id_returns_404(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/show",
            body={"history_id": "01NOSUCHITEM"},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 404


def test_show_rejects_malformed_id(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    # Path-escape attempt — id must match the ULID-shaped alphabet.
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/show",
            body={"history_id": "../../etc/passwd"},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 400


def test_show_missing_field_returns_400(secrets, s3_bucket):
    login = _login()
    token = _session_cookie_value(login)
    resp = admin_api.handler(
        _event(
            "POST",
            "/admin/show",
            body={},
            cookies=[f"einkgen_admin={token}"],
        )
    )
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_unknown_path_returns_404(secrets):
    resp = admin_api.handler(_event("GET", "/admin/whatever"))
    assert resp["statusCode"] == 404


def test_wrong_method_on_known_path_returns_404(secrets):
    # GET /admin/login isn't a route — dispatcher returns 404, not 405.
    resp = admin_api.handler(_event("GET", "/admin/login"))
    assert resp["statusCode"] == 404
