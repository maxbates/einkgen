"""Tests for the read-api Lambda entrypoint (Function URL v2.0)."""

from __future__ import annotations

import json
import time
from typing import Any

from einkgen.core import queue as q
from einkgen.lambdas import read_api
from tests.conftest import TEST_BUCKET


def _event(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal Lambda Function URL v2.0 event."""
    return {
        "version": "2.0",
        "rawPath": path,
        "queryStringParameters": query,
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
            }
        },
        "headers": {},
    }


def _body(resp: dict[str, Any]) -> Any:
    return json.loads(resp["body"])


def _put_history_manifest(
    client,
    item_id: str,
    generated_at: str,
    *,
    source: dict[str, Any] | None = None,
    image_sha256: str = "f" * 64,
) -> None:
    body = json.dumps(
        {
            "version": 1,
            "generated_at": generated_at,
            "image_url": "https://cdn.test.example.com/current/image.bmp",
            "image_sha256": image_sha256,
            "image_bytes": 100,
            "display": {"width": 1200, "height": 825, "levels": 8},
            "next_check_after": "2026-05-13T16:05:00Z",
            "source": source or {"kind": "generated", "prompt": "p"},
        }
    ).encode("utf-8")
    client.put_object(
        Bucket=TEST_BUCKET,
        Key=f"history/{item_id}/manifest.json",
        Body=body,
        ContentType="application/json",
    )


def _put_status(client, device_id: str, payload: dict[str, Any]) -> None:
    client.put_object(
        Bucket=TEST_BUCKET,
        Key=f"status/device-{device_id}.json",
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )


def test_queue_empty(s3_bucket):
    resp = read_api.handler(_event("GET", "/queue"))
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    assert _body(resp) == {"items": []}


def test_queue_returns_items_in_fifo_order(s3_bucket):
    a = q.enqueue("prompt", prompt="first")
    b = q.enqueue("prompt", prompt="second")
    c = q.enqueue("prompt", prompt="third")

    resp = read_api.handler(_event("GET", "/queue"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    ids = [item["id"] for item in body["items"]]
    assert ids == [a.id, b.id, c.id]
    # Serialized form must not leak internal _s3_key.
    for item in body["items"]:
        assert "_s3_key" not in item
    # Sanity: the public schema fields are present.
    assert body["items"][0]["prompt"] == "first"
    assert body["items"][0]["kind"] == "prompt"


def test_queue_ignores_staged_prefix(s3_bucket):
    q.enqueue("prompt", prompt="pending")
    # A staged image lives under queue/staged/ and must not appear in the listing.
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key="queue/staged/abc123.jpg",
        Body=b"\xff\xd8\xff",
        ContentType="image/jpeg",
    )

    resp = read_api.handler(_event("GET", "/queue"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert len(body["items"]) == 1
    assert body["items"][0]["prompt"] == "pending"


def test_history_empty(s3_bucket):
    resp = read_api.handler(_event("GET", "/history"))
    assert resp["statusCode"] == 200
    assert _body(resp) == {"items": []}


def test_history_newest_first(s3_bucket):
    _put_history_manifest(
        s3_bucket, "id-old", "2026-05-13T10:00:00Z",
        source={"kind": "generated", "prompt": "old"},
        image_sha256="a" * 64,
    )
    _put_history_manifest(
        s3_bucket, "id-mid", "2026-05-13T12:00:00Z",
        source={"kind": "upload", "image_s3_key": "queue/staged/x.jpg"},
        image_sha256="b" * 64,
    )
    _put_history_manifest(
        s3_bucket, "id-new", "2026-05-13T14:00:00Z",
        source={"kind": "generated", "prompt": "new"},
        image_sha256="c" * 64,
    )

    resp = read_api.handler(_event("GET", "/history"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert [e["id"] for e in body["items"]] == ["id-new", "id-mid", "id-old"]
    assert body["items"][0]["generated_at"] == "2026-05-13T14:00:00Z"
    assert body["items"][0]["image_sha256"] == "c" * 64
    assert body["items"][0]["source"] == {"kind": "generated", "prompt": "new"}
    assert body["items"][1]["source"]["image_s3_key"] == "queue/staged/x.jpg"


def test_history_honors_limit_query(s3_bucket):
    for i in range(5):
        _put_history_manifest(
            s3_bucket, f"id-{i}", f"2026-05-13T{10 + i:02d}:00:00Z",
        )
    resp = read_api.handler(_event("GET", "/history", query={"limit": "2"}))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert [e["id"] for e in body["items"]] == ["id-4", "id-3"]


def test_history_bad_limit_falls_back_to_default(s3_bucket):
    for i in range(3):
        _put_history_manifest(
            s3_bucket, f"id-{i}", f"2026-05-13T{10 + i:02d}:00:00Z",
        )
    # "not-a-number" → default 50, which is >= the 3 items present.
    resp = read_api.handler(
        _event("GET", "/history", query={"limit": "not-a-number"})
    )
    assert resp["statusCode"] == 200
    assert len(_body(resp)["items"]) == 3

    # Missing param: same behaviour.
    resp = read_api.handler(_event("GET", "/history", query=None))
    assert resp["statusCode"] == 200
    assert len(_body(resp)["items"]) == 3

    # Non-positive: also default.
    resp = read_api.handler(_event("GET", "/history", query={"limit": "-5"}))
    assert resp["statusCode"] == 200
    assert len(_body(resp)["items"]) == 3


def test_history_limit_caps_at_500(s3_bucket, monkeypatch):
    # Avoid actually seeding 600 entries; just confirm the parser caps.
    event = _event("GET", "/history", query={"limit": "100000"})
    limit = read_api._parse_limit(
        event,
        default=read_api.DEFAULT_HISTORY_LIMIT,
        maximum=read_api.MAX_HISTORY_LIMIT,
    )
    assert limit == read_api.MAX_HISTORY_LIMIT == 500


def test_queue_limit_query_truncates(s3_bucket):
    for _ in range(5):
        q.enqueue("prompt", prompt="x")
    resp = read_api.handler(_event("GET", "/queue", query={"limit": "2"}))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert len(body["items"]) == 2


def test_history_drops_entries_with_empty_generated_at(s3_bucket):
    # A malformed manifest without generated_at must not appear in /history;
    # otherwise it sorts to the bottom and adds a phantom row.
    _put_history_manifest(s3_bucket, "id-ok", "2026-05-13T10:00:00Z")
    # Hand-write a broken manifest (no generated_at key).
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key="history/id-broken/manifest.json",
        Body=json.dumps({"image_sha256": "x" * 64}).encode("utf-8"),
        ContentType="application/json",
    )
    resp = read_api.handler(_event("GET", "/history"))
    body = _body(resp)
    assert [e["id"] for e in body["items"]] == ["id-ok"]


def test_status_404_when_empty(s3_bucket):
    resp = read_api.handler(_event("GET", "/status"))
    assert resp["statusCode"] == 404
    assert _body(resp) == {"error": "no_status_yet"}


def test_status_returns_single_device(s3_bucket):
    payload = {
        "battery_v": 3.8,
        "battery_pct": 75,
        "rssi": -60,
        "last_seen": "2026-05-13T14:00:00Z",
        "current_hash": "deadbeef",
        "fw_version": "0.1.1",
    }
    _put_status(s3_bucket, "01ALPHA", payload)

    resp = read_api.handler(_event("GET", "/status"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["device_id"] == "01ALPHA"
    assert body["battery_v"] == 3.8
    assert body["battery_pct"] == 75
    assert body["rssi"] == -60
    assert body["current_hash"] == "deadbeef"
    assert body["fw_version"] == "0.1.1"
    assert body["last_seen"] == "2026-05-13T14:00:00Z"
    assert "last_modified" in body and body["last_modified"]


def test_status_picks_newest_by_last_modified(s3_bucket):
    _put_status(
        s3_bucket,
        "01OLD",
        {"battery_v": 3.5, "battery_pct": 40, "last_seen": "2026-05-13T10:00:00Z"},
    )
    time.sleep(0.01)
    _put_status(
        s3_bucket,
        "01NEW",
        {"battery_v": 3.9, "battery_pct": 88, "last_seen": "2026-05-13T14:00:00Z"},
    )

    resp = read_api.handler(_event("GET", "/status"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["device_id"] == "01NEW"
    assert body["battery_pct"] == 88


def test_devices_returns_empty_list_when_no_reports(s3_bucket):
    resp = read_api.handler(_event("GET", "/devices"))
    assert resp["statusCode"] == 200
    assert _body(resp) == {"items": []}


def test_devices_lists_all_reports_newest_first(s3_bucket):
    # moto's S3 LastModified is at 1-second resolution, so we need
    # >=1s between puts to get a deterministic newest-first ordering.
    _put_status(
        s3_bucket,
        "01OLD",
        {"battery_v": 3.5, "battery_pct": 40, "last_seen": "2026-05-13T10:00:00Z"},
    )
    time.sleep(1.1)
    _put_status(
        s3_bucket,
        "01NEW",
        {"battery_v": 3.9, "battery_pct": 88, "last_seen": "2026-05-13T14:00:00Z"},
    )
    time.sleep(1.1)
    _put_status(
        s3_bucket,
        "kitchen",
        {"battery_v": 4.1, "battery_pct": 95, "last_seen": "2026-05-13T16:00:00Z"},
    )

    resp = read_api.handler(_event("GET", "/devices"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    ids = [item["device_id"] for item in body["items"]]
    assert ids == ["kitchen", "01NEW", "01OLD"]
    for item in body["items"]:
        assert "last_modified" in item and item["last_modified"]


def test_post_returns_405(s3_bucket):
    resp = read_api.handler(_event("POST", "/queue"))
    assert resp["statusCode"] == 405
    assert _body(resp) == {"error": "method_not_allowed"}


def test_unknown_path_returns_404(s3_bucket):
    resp = read_api.handler(_event("GET", "/nope"))
    assert resp["statusCode"] == 404
    assert _body(resp) == {"error": "not_found"}


def test_trailing_slash_is_normalized(s3_bucket):
    # /queue/ and /queue should behave the same — Function URL clients are
    # not always strict about the trailing slash.
    resp = read_api.handler(_event("GET", "/queue/"))
    assert resp["statusCode"] == 200
    assert _body(resp) == {"items": []}


def test_unexpected_error_returns_500(s3_bucket, monkeypatch):
    def boom(_prefix: str):
        raise RuntimeError("s3 down")

    monkeypatch.setattr("einkgen.lambdas.read_api.s3.list_objects", boom)
    resp = read_api.handler(_event("GET", "/history"))
    assert resp["statusCode"] == 500
    assert _body(resp) == {"error": "internal"}
    # Defensive headers still set on errors.
    assert resp["headers"]["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# /generated — pre-rendered buffer
# ---------------------------------------------------------------------------


def test_generated_empty(s3_bucket):
    resp = read_api.handler(_event("GET", "/generated"))
    assert resp["statusCode"] == 200
    assert _body(resp) == {"items": []}


def test_generated_returns_items_in_fifo_order(s3_bucket):
    from einkgen.core import generated_queue

    a = generated_queue.enqueue(
        "01HAAA00000000000000000000",
        image_sha256="a" * 64,
        image_bytes=1,
        source={"kind": "generated", "prompt": "first"},
    )
    b = generated_queue.enqueue(
        "01HBBB00000000000000000000",
        image_sha256="b" * 64,
        image_bytes=1,
        source={"kind": "generated", "prompt": "second"},
    )

    resp = read_api.handler(_event("GET", "/generated"))
    assert resp["statusCode"] == 200
    body = _body(resp)
    ids = [it["history_id"] for it in body["items"]]
    assert ids == [a.history_id, b.history_id]
    # Serialised form doesn't leak the internal s3 key.
    for item in body["items"]:
        assert "_s3_key" not in item
    assert body["items"][0]["source"]["prompt"] == "first"


def test_generated_limit_truncates(s3_bucket):
    from einkgen.core import generated_queue

    for i in range(5):
        generated_queue.enqueue(
            f"01H{i:023d}",
            image_sha256="a" * 64,
            image_bytes=1,
            source={"kind": "generated"},
        )

    resp = read_api.handler(_event("GET", "/generated", query={"limit": "3"}))
    assert resp["statusCode"] == 200
    items = _body(resp)["items"]
    assert len(items) == 3
