"""Tests for the device-status Lambda entrypoint.

We don't share a Secrets Manager fixture with the rest of the suite because
no other test needs it. Each test that exercises the real auth path creates
the secret inside the `s3_bucket` fixture's `mock_aws` context (mock_aws
mocks every service uniformly), and resets the module-scope token cache
in a teardown.
"""

from __future__ import annotations

import base64
import json
import re
from unittest.mock import MagicMock

import boto3
import pytest

from einkgen.lambdas import device_status

TEST_TOKEN = "s3cret-device-token"
TEST_SECRET_NAME = "einkgen-test/device_status_token"
TEST_BUCKET = "einkgen-test"


@pytest.fixture
def reset_cache():
    """Drop the module-level token cache before AND after each test.

    Before: tests must not see a token cached by a prior test.
    After:  later tests (incl. those that don't use this fixture) get a clean slate.
    """
    device_status._reset_cache()
    yield
    device_status._reset_cache()


@pytest.fixture
def secret(s3_bucket, monkeypatch, reset_cache):
    """Create the device-status secret inside the same mock_aws context as S3.

    Returns the secret name so individual tests can also patch the token
    directly when they want to bypass Secrets Manager entirely.
    """
    monkeypatch.setenv("DEVICE_STATUS_SECRET_NAME", TEST_SECRET_NAME)
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(Name=TEST_SECRET_NAME, SecretString=TEST_TOKEN)
    return TEST_SECRET_NAME


def _event(
    body: str | None,
    *,
    method: str = "POST",
    token: str | None = TEST_TOKEN,
    is_base64: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    headers: dict[str, str] = {"content-type": "application/json"}
    if token is not None:
        headers["x-device-token"] = token
    if extra_headers:
        headers.update(extra_headers)
    event: dict = {
        "version": "2.0",
        "rawPath": "/",
        "requestContext": {"http": {"method": method}},
        "headers": headers,
        "isBase64Encoded": is_base64,
    }
    if body is not None:
        event["body"] = body
    return event


def _get_status_object(s3_client, device_id: str) -> dict:
    key = f"status/device-{device_id}.json"
    resp = s3_client.get_object(Bucket=TEST_BUCKET, Key=key)
    return json.loads(resp["Body"].read())


def _list_status_keys(s3_client) -> list[str]:
    resp = s3_client.list_objects_v2(Bucket=TEST_BUCKET, Prefix="status/")
    return [obj["Key"] for obj in resp.get("Contents", []) or []]


ISO8601_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_happy_path_writes_status_object(s3_bucket, secret):
    body = json.dumps(
        {
            "device_id": "kitchen",
            "battery_v": 4.05,
            "battery_pct": 83,
            "rssi": -52,
            "current_hash": "abc123",
            "fw_version": "v0.1.0",
        }
    )
    resp = device_status.handler(_event(body))

    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    assert json.loads(resp["body"]) == {"ok": True, "device_id": "kitchen"}

    record = _get_status_object(s3_bucket, "kitchen")
    assert record["device_id"] == "kitchen"
    assert record["battery_v"] == 4.05
    assert record["battery_pct"] == 83
    assert record["rssi"] == -52
    assert record["current_hash"] == "abc123"
    assert record["fw_version"] == "v0.1.0"
    assert ISO8601_UTC.match(record["last_seen"])


def test_token_is_cached_across_invocations(s3_bucket, secret, monkeypatch):
    # Spy on the underlying SM client's get_secret_value to confirm it's
    # only called once for two consecutive valid POSTs.
    real_client = device_status._get_sm_client()
    spy = MagicMock(wraps=real_client.get_secret_value)
    monkeypatch.setattr(real_client, "get_secret_value", spy)
    # Force the first call to actually hit SM by clearing any cached token.
    device_status._reset_cache()
    # Re-pin the same client so our spy sticks even after reset.
    device_status._sm_client = real_client

    body = json.dumps({"device_id": "alpha", "battery_v": 4.0})

    r1 = device_status.handler(_event(body))
    r2 = device_status.handler(_event(body))

    assert r1["statusCode"] == 200
    assert r2["statusCode"] == 200
    assert spy.call_count == 1


def test_missing_token_returns_401_and_no_write(s3_bucket, secret):
    body = json.dumps({"device_id": "alpha"})
    resp = device_status.handler(_event(body, token=None))

    assert resp["statusCode"] == 401
    assert json.loads(resp["body"]) == {"error": "unauthorized"}
    assert _list_status_keys(s3_bucket) == []


def test_wrong_token_returns_401_and_no_write(s3_bucket, secret):
    body = json.dumps({"device_id": "alpha"})
    resp = device_status.handler(_event(body, token="not-the-token"))

    assert resp["statusCode"] == 401
    assert json.loads(resp["body"]) == {"error": "unauthorized"}
    assert _list_status_keys(s3_bucket) == []


def test_get_method_returns_400(s3_bucket, secret):
    resp = device_status.handler(_event(None, method="GET"))

    assert resp["statusCode"] == 400
    payload = json.loads(resp["body"])
    assert payload["error"] == "bad_request"
    assert "POST" in payload["detail"]
    assert _list_status_keys(s3_bucket) == []


def test_malformed_json_body_returns_400(s3_bucket, secret):
    resp = device_status.handler(_event("{not json"))

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "bad_request"
    assert _list_status_keys(s3_bucket) == []


def test_empty_body_returns_400(s3_bucket, secret):
    resp = device_status.handler(_event(""))

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "bad_request"
    assert _list_status_keys(s3_bucket) == []


def test_missing_device_id_falls_back_to_default(s3_bucket, secret):
    # Matches current firmware which doesn't send device_id (see docstring).
    body = json.dumps(
        {"battery_v": 3.95, "battery_pct": 72, "rssi": -60, "fw_version": "v0.1.0"}
    )
    resp = device_status.handler(_event(body))

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"ok": True, "device_id": "default"}

    record = _get_status_object(s3_bucket, "default")
    assert record["device_id"] == "default"
    assert record["battery_v"] == 3.95


def test_base64_encoded_body_is_decoded(s3_bucket, secret):
    inner = json.dumps({"device_id": "b64", "battery_v": 4.1}).encode("utf-8")
    encoded = base64.b64encode(inner).decode("ascii")
    resp = device_status.handler(_event(encoded, is_base64=True))

    assert resp["statusCode"] == 200
    record = _get_status_object(s3_bucket, "b64")
    assert record["battery_v"] == 4.1


def test_same_device_overwrites(s3_bucket, secret):
    body1 = json.dumps({"device_id": "kitchen", "battery_v": 4.10, "rssi": -50})
    body2 = json.dumps({"device_id": "kitchen", "battery_v": 3.85, "rssi": -70})

    device_status.handler(_event(body1))
    device_status.handler(_event(body2))

    keys = _list_status_keys(s3_bucket)
    assert keys == ["status/device-kitchen.json"]
    record = _get_status_object(s3_bucket, "kitchen")
    assert record["battery_v"] == 3.85
    assert record["rssi"] == -70


def test_different_devices_get_separate_keys(s3_bucket, secret):
    device_status.handler(
        _event(json.dumps({"device_id": "kitchen", "battery_v": 4.0}))
    )
    device_status.handler(
        _event(json.dumps({"device_id": "office", "battery_v": 3.9}))
    )

    keys = sorted(_list_status_keys(s3_bucket))
    assert keys == [
        "status/device-kitchen.json",
        "status/device-office.json",
    ]
    assert _get_status_object(s3_bucket, "kitchen")["battery_v"] == 4.0
    assert _get_status_object(s3_bucket, "office")["battery_v"] == 3.9


def test_uppercase_header_name_is_accepted(s3_bucket, secret):
    # Function URLs lowercase headers, but a direct invocation (e.g. local
    # testing) may pass the original casing — make sure we still accept it.
    body = json.dumps({"device_id": "alpha"})
    event = _event(body, token=None)
    event["headers"]["X-Device-Token"] = TEST_TOKEN

    resp = device_status.handler(event)
    assert resp["statusCode"] == 200
