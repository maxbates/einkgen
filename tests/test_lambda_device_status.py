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
    path: str = "/",
) -> dict:
    headers: dict[str, str] = {"content-type": "application/json"}
    if token is not None:
        headers["x-device-token"] = token
    if extra_headers:
        headers.update(extra_headers)
    event: dict = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
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
    # device-status is firmware-only, so we no longer advertise a CORS
    # Access-Control-Allow-Origin header — the SPA never calls this
    # Lambda. Asserting absence keeps the doc/code-intent honest.
    assert "Access-Control-Allow-Origin" not in resp["headers"]
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


def test_non_dict_json_body_returns_400(s3_bucket, secret):
    # JSON parses but isn't an object — array, number, string, null all hit
    # the `isinstance(body, dict)` guard inside the handler.
    for raw in ("[1, 2, 3]", "123", "\"hello\"", "null"):
        resp = device_status.handler(_event(raw))
        assert resp["statusCode"] == 400, raw
        assert json.loads(resp["body"])["error"] == "bad_request", raw
    assert _list_status_keys(s3_bucket) == []


def test_body_size_cap_returns_413(s3_bucket, secret):
    # 4 KB cap; pad with a long string field so the body decodes but is too big.
    huge = json.dumps({"device_id": "x", "noise": "A" * (5 * 1024)})
    resp = device_status.handler(_event(huge))
    assert resp["statusCode"] == 413
    assert json.loads(resp["body"])["error"] == "bad_request"
    assert _list_status_keys(s3_bucket) == []


def test_invalid_device_id_returns_400(s3_bucket, secret):
    # Slashes / unicode / overly-long ids must be rejected, not silently
    # rewritten — they'd produce surprising S3 keys.
    for bad in ("../../evil", "device id with spaces", "x" * 100, "drop\ttab"):
        resp = device_status.handler(
            _event(json.dumps({"device_id": bad, "battery_v": 4.0}))
        )
        assert resp["statusCode"] == 400, bad
        assert json.loads(resp["body"])["error"] == "bad_request", bad
    assert _list_status_keys(s3_bucket) == []


def test_record_drops_unknown_fields(s3_bucket, secret):
    # A token-holder posting extra junk must not get it persisted.
    body = json.dumps(
        {
            "device_id": "kitchen",
            "battery_v": 3.9,
            "battery_pct": 80,
            "noise": "x" * 200,
            "evil": [1, 2, 3],
            "huge": 1e100,
        }
    )
    resp = device_status.handler(_event(body))
    assert resp["statusCode"] == 200

    record = _get_status_object(s3_bucket, "kitchen")
    assert record["battery_v"] == 3.9
    assert record["battery_pct"] == 80
    assert "noise" not in record
    assert "evil" not in record
    assert "huge" not in record


def test_secrets_manager_failure_returns_401(s3_bucket, secret, monkeypatch):
    # Simulate SM outage. The handler must fail closed (401, no S3 write) so
    # an attacker can't distinguish "wrong token" from "auth backend down".
    device_status._reset_cache()

    class FailingClient:
        def get_secret_value(self, **_):
            raise RuntimeError("secretsmanager unreachable")

    monkeypatch.setattr(device_status, "_get_sm_client", lambda: FailingClient())
    resp = device_status.handler(
        _event(json.dumps({"device_id": "alpha"}), token="anything")
    )
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"]) == {"error": "unauthorized"}
    assert _list_status_keys(s3_bucket) == []


def test_uppercase_header_name_is_accepted(s3_bucket, secret):
    # Function URLs lowercase headers, but a direct invocation (e.g. local
    # testing) may pass the original casing — make sure we still accept it.
    body = json.dumps({"device_id": "alpha"})
    event = _event(body, token=None)
    event["headers"]["X-Device-Token"] = TEST_TOKEN

    resp = device_status.handler(event)
    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# POST /wake — sha-debounced advance from the generated buffer
# ---------------------------------------------------------------------------

import boto3 as _boto3  # noqa: E402

from einkgen.core import generated_queue, publish  # noqa: E402

# Realistic-looking sha256s for branch assertions. Length-64 lowercase hex.
SHA_CURRENT = "f" * 64
SHA_DEVICE_STALE = "e" * 64
SHA_HISTORY = "abc1234" + "0" * 57


def _wake_event(
    body: str | None,
    *,
    token: str | None = TEST_TOKEN,
    method: str = "POST",
) -> dict:
    return _event(body, token=token, method=method, path="/wake")


def _seed_current_manifest(s3_bucket, sha: str = SHA_CURRENT) -> None:
    """Write a minimal current/manifest.json that the /wake handler reads."""
    body = json.dumps(
        {
            "version": 1,
            "generated_at": "2026-05-18T00:00:00Z",
            "image_url": "https://cdn.example.com/current/image.bmp",
            "image_sha256": sha,
            "image_bytes": 12345,
            "display": {"width": 1200, "height": 825, "levels": 8},
            "next_check_after": "2026-05-18T00:30:00Z",
            "source": {"kind": "generated"},
        }
    ).encode("utf-8")
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=publish.CURRENT_MANIFEST_KEY, Body=body
    )


def _seed_history(s3_bucket, history_id: str, sha: str = SHA_HISTORY) -> None:
    """Write history/<id>/manifest.json so set_current_from_history works."""
    body = json.dumps(
        {
            "version": 1,
            "generated_at": "2026-05-18T00:00:00Z",
            "image_url": f"https://cdn.example.com/history/{history_id}/processed.bmp",
            "image_sha256": sha,
            "image_bytes": 999,
            "display": {"width": 1200, "height": 825, "levels": 8},
            "next_check_after": "2026-05-18T00:30:00Z",
            "source": {"kind": "generated", "prompt": "a topic"},
        }
    ).encode("utf-8")
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key=f"history/{history_id}/manifest.json",
        Body=body,
    )


def _enqueue_marker(history_id: str, sha: str = SHA_HISTORY) -> None:
    generated_queue.enqueue(
        history_id,
        image_sha256=sha,
        image_bytes=999,
        source={"kind": "generated", "prompt": "a topic"},
    )


def _stub_replenish(monkeypatch):
    """Capture replenish invokes without actually calling Lambda."""
    calls: list[dict] = []

    def fake_invoke(payload):
        calls.append(payload)

    monkeypatch.setattr(device_status, "_fire_replenish", lambda: fake_invoke({"action": "render_one"}))
    return calls


def test_wake_unauth_returns_401(s3_bucket, secret):
    body = json.dumps({"current_sha256": SHA_CURRENT})
    resp = device_status.handler(_wake_event(body, token=None))
    assert resp["statusCode"] == 401


def test_wake_get_method_rejected(s3_bucket, secret):
    resp = device_status.handler(_wake_event(None, method="GET"))
    assert resp["statusCode"] == 400


def test_wake_malformed_sha_returns_400(s3_bucket, secret):
    body = json.dumps({"current_sha256": "not-hex"})
    resp = device_status.handler(_wake_event(body))
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "bad_request"


def test_wake_redraw_when_device_sha_differs(s3_bucket, secret, monkeypatch):
    """Mismatch → tell device to redraw existing manifest; don't pop buffer.

    The response embeds the current manifest fields so the firmware can
    skip the follow-up GET that would otherwise hit CloudFront's cache.
    """
    _seed_current_manifest(s3_bucket, sha=SHA_CURRENT)
    _enqueue_marker("01HHHHHHHHHHHHHHHHHHHHHHHH")
    invokes = _stub_replenish(monkeypatch)

    body = json.dumps({"current_sha256": SHA_DEVICE_STALE})
    resp = device_status.handler(_wake_event(body))

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["action"] == "redraw"
    assert payload["manifest_sha256"] == SHA_CURRENT
    # Embedded manifest fields — firmware uses these to skip the GET.
    assert payload["image_url"] == "https://cdn.example.com/current/image.bmp"
    assert payload["image_sha256"] == SHA_CURRENT
    assert payload["image_bytes"] == 12345
    assert payload["next_check_after"] == "2026-05-18T00:30:00Z"
    # Buffer untouched.
    assert generated_queue.count() == 1
    # No replenish fired — we didn't advance.
    assert invokes == []


def test_wake_advance_pops_head_and_sets_current(s3_bucket, secret, monkeypatch):
    """Match + non-empty buffer → pop, set_current_from_history, fire replenish.

    The response embeds the freshly-published manifest fields so the
    firmware can skip the follow-up GET (which would hit a CloudFront
    cache still pointing at the pre-advance manifest).
    """
    _seed_current_manifest(s3_bucket, sha=SHA_CURRENT)
    history_id = "01HHHHHHHHHHHHHHHHHHHHHHHH"
    history_sha = "1" + "a" * 63
    _seed_history(s3_bucket, history_id, sha=history_sha)
    _enqueue_marker(history_id, sha=history_sha)
    invokes = _stub_replenish(monkeypatch)

    body = json.dumps({"current_sha256": SHA_CURRENT})
    resp = device_status.handler(_wake_event(body))

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["action"] == "advance"
    assert payload["history_id"] == history_id
    assert payload["manifest_sha256"] == history_sha
    # Embedded manifest fields point at the just-promoted history frame.
    assert payload["image_url"].endswith(f"/history/{history_id}/processed.bmp")
    assert payload["image_sha256"] == history_sha
    assert payload["image_bytes"] == 999
    assert ISO8601_UTC.match(payload["next_check_after"])
    # Marker was finalized; buffer is empty now.
    assert generated_queue.empty()
    # Replenish fired exactly once.
    assert len(invokes) == 1
    # current/manifest.json was rewritten with the new sha.
    new_manifest = json.loads(
        s3_bucket.get_object(Bucket=TEST_BUCKET, Key=publish.CURRENT_MANIFEST_KEY)[
            "Body"
        ].read()
    )
    assert new_manifest["image_sha256"] == history_sha
    assert new_manifest["source"]["replayed_from"] == history_id
    # The embedded fields agree with what landed in S3.
    assert payload["image_url"] == new_manifest["image_url"]
    assert payload["next_check_after"] == new_manifest["next_check_after"]


def test_wake_queue_empty_when_buffer_drained(s3_bucket, secret, monkeypatch):
    """Match + empty buffer → noop, don't burn a fresh OpenAI call.

    No manifest fields are embedded — the firmware keeps drawing what
    it already has and falls back to fetchManifest on the next wake
    (where the cache will have caught up).
    """
    _seed_current_manifest(s3_bucket, sha=SHA_CURRENT)
    invokes = _stub_replenish(monkeypatch)

    body = json.dumps({"current_sha256": SHA_CURRENT})
    resp = device_status.handler(_wake_event(body))

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["action"] == "queue_empty"
    assert payload["manifest_sha256"] == SHA_CURRENT
    assert "image_url" not in payload
    assert "image_sha256" not in payload
    assert "image_bytes" not in payload
    assert "next_check_after" not in payload
    assert invokes == []


def test_wake_first_deploy_no_current_manifest_advances(s3_bucket, secret, monkeypatch):
    """No current manifest yet + non-empty buffer → advance."""
    # Deliberately do NOT seed current/manifest.json.
    history_id = "01HFRESHDEPLOYHFRESHDEPLOY"
    history_sha = "2" + "b" * 63
    _seed_history(s3_bucket, history_id, sha=history_sha)
    _enqueue_marker(history_id, sha=history_sha)
    invokes = _stub_replenish(monkeypatch)

    body = json.dumps({"current_sha256": ""})  # firmware NVS is empty
    resp = device_status.handler(_wake_event(body))

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["action"] == "advance"
    assert payload["history_id"] == history_id
    # Embedded manifest fields are present on the fresh-deploy advance.
    assert payload["image_url"].endswith(f"/history/{history_id}/processed.bmp")
    assert payload["image_sha256"] == history_sha
    assert payload["image_bytes"] == 999
    assert ISO8601_UTC.match(payload["next_check_after"])
    assert generated_queue.empty()
    assert len(invokes) == 1


def test_wake_advance_when_current_manifest_malformed(s3_bucket, secret, monkeypatch):
    """Garbage at current/manifest.json → treated as missing → advance branch.

    Same UX as a fresh deploy. Guards against a malformed manifest
    (mid-write crash, partial S3 put) stranding the device in the
    redraw branch.
    """
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key=publish.CURRENT_MANIFEST_KEY,
        Body=b"this is not json",
    )
    history_id = "01HMALFORMED01HMALFORMED01"
    history_sha = "3" + "c" * 63
    _seed_history(s3_bucket, history_id, sha=history_sha)
    _enqueue_marker(history_id, sha=history_sha)
    invokes = _stub_replenish(monkeypatch)

    body = json.dumps({"current_sha256": SHA_DEVICE_STALE})
    resp = device_status.handler(_wake_event(body))

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["action"] == "advance"
    assert payload["history_id"] == history_id
    assert payload["image_sha256"] == history_sha
    assert payload["image_bytes"] == 999
    assert generated_queue.empty()
    assert len(invokes) == 1


def test_wake_marker_pointing_at_missing_history_drops_marker(
    s3_bucket, secret, monkeypatch
):
    """A stale marker (no history archive) advances the buffer without crashing."""
    _seed_current_manifest(s3_bucket, sha=SHA_CURRENT)
    _enqueue_marker("01HMISSINGHISTORY01HMISSING")  # no history archive seeded
    invokes = _stub_replenish(monkeypatch)

    body = json.dumps({"current_sha256": SHA_CURRENT})
    resp = device_status.handler(_wake_event(body))

    assert resp["statusCode"] == 200
    payload = json.loads(resp["body"])
    assert payload["action"] == "queue_empty"
    # Falls through the queue_empty branch — no embedded manifest.
    assert "image_url" not in payload
    assert "image_sha256" not in payload
    # Marker was dropped so we don't hit the same fault on the next wake.
    assert generated_queue.empty()
    # No replenish fired — we didn't actually advance.
    assert invokes == []


def test_wake_status_route_still_works(s3_bucket, secret):
    """Adding /wake didn't break the existing status heartbeat."""
    body = json.dumps({"device_id": "kitchen", "battery_v": 4.0, "current_hash": "x"})
    resp = device_status.handler(_event(body, path="/"))
    assert resp["statusCode"] == 200
