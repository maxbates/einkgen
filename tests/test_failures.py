"""Tests for ``einkgen.core.failures`` — the rejection breadcrumb store."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from einkgen.core import failures, s3
from einkgen.core.queue import QueueItem
from tests.conftest import TEST_BUCKET


def _make_item(
    *, item_id: str = "01HFTEST0001", prompt: str | None = "the prompt"
) -> QueueItem:
    return QueueItem(
        id=item_id,
        enqueued_at="2026-05-17T17:53:30Z",
        source="admin",
        kind="prompt",
        prompt=prompt,
    )


def test_record_then_list_round_trips(s3_bucket):
    item = _make_item()
    failures.record(item, "moderation_blocked")
    out = failures.list_recent()
    assert len(out) == 1
    rec = out[0]
    assert rec.id == "01HFTEST0001"
    assert rec.prompt == "the prompt"
    assert rec.reason == "moderation_blocked"
    assert rec.source == "admin"
    assert rec.kind == "prompt"


def test_records_sorted_newest_first(s3_bucket, monkeypatch):
    """The Admin tab shows the most recent drop at the top.

    Recorded_at must stay inside the visibility window, so anchor the
    fake timestamps on wall-clock now and only differ by seconds.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    times = iter(
        [
            (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
    )
    monkeypatch.setattr(failures, "_iso_now", lambda: next(times))

    failures.record(_make_item(item_id="OLDEST", prompt="a"), "r1")
    failures.record(_make_item(item_id="MIDDLE", prompt="b"), "r2")
    failures.record(_make_item(item_id="NEWEST", prompt="c"), "r3")

    out = failures.list_recent()
    assert [r.id for r in out] == ["NEWEST", "MIDDLE", "OLDEST"]


def test_old_records_hidden_on_read(s3_bucket):
    """Anything older than MAX_AGE_SECONDS is filtered out on read.

    Use a record whose recorded_at is hours in the past — list_recent
    should drop it regardless of S3 LastModified.
    """
    long_ago = (
        datetime.now(timezone.utc) - timedelta(seconds=failures.MAX_AGE_SECONDS + 600)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = f"{failures.FAILURE_PREFIX}{long_ago.replace(':', '-')}-EXPIRED.json"
    body = json.dumps(
        {
            "id": "EXPIRED",
            "enqueued_at": long_ago,
            "recorded_at": long_ago,
            "source": "admin",
            "kind": "prompt",
            "prompt": "stale",
            "reason": "should not appear",
        }
    ).encode()
    s3.put_object(key, body, content_type="application/json")

    assert failures.list_recent() == []


def test_record_truncates_long_prompt_and_reason(s3_bucket):
    long_prompt = "x" * (failures.MAX_PROMPT_CHARS + 50)
    long_reason = "y" * (failures.MAX_REASON_CHARS + 50)
    item = _make_item(prompt=long_prompt)
    failures.record(item, long_reason)
    out = failures.list_recent()
    assert len(out) == 1
    # We cap to MAX_*_CHARS (truncation includes a trailing ellipsis).
    assert out[0].prompt is not None
    assert len(out[0].prompt) == failures.MAX_PROMPT_CHARS
    assert len(out[0].reason) == failures.MAX_REASON_CHARS


def test_sweep_expired_uses_s3_last_modified(s3_bucket, monkeypatch):
    """``_sweep_expired`` deletes S3 objects with LastModified < cutoff.

    Drive the function with a fake ``s3.list_objects`` so we control
    LastModified directly — that's the field sweep keys off, and moto
    can't backdate a real S3 PutObject.
    """
    real_now = datetime.now(timezone.utc)
    fake_objects = [
        {"Key": "queue/failed/old-A.json", "LastModified": real_now - timedelta(hours=2), "Size": 100},
        {"Key": "queue/failed/old-B.json", "LastModified": real_now - timedelta(seconds=failures.MAX_AGE_SECONDS + 5), "Size": 100},
        {"Key": "queue/failed/fresh.json", "LastModified": real_now - timedelta(seconds=10), "Size": 100},
    ]
    deleted: list[str] = []
    monkeypatch.setattr(s3, "list_objects", lambda _prefix: fake_objects)
    monkeypatch.setattr(s3, "delete_object", lambda key: deleted.append(key))

    failures._sweep_expired()

    # Both old-A and old-B are past the cutoff; fresh survives.
    assert set(deleted) == {"queue/failed/old-A.json", "queue/failed/old-B.json"}


def test_record_triggers_sweep(s3_bucket, monkeypatch):
    """record() invokes _sweep_expired so the prefix self-cleans on writes."""
    swept: list[bool] = []
    monkeypatch.setattr(failures, "_sweep_expired", lambda: swept.append(True))

    failures.record(_make_item(), "r")

    assert swept == [True]


def test_record_swallows_s3_write_failure(s3_bucket, monkeypatch):
    """A failed S3 write must not raise — the queue can't be pinned by a
    notification side channel."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated S3 outage")

    monkeypatch.setattr(s3, "put_object", boom)

    # Should not raise.
    failures.record(_make_item(), "this write will fail")


def test_list_recent_caps_returned_count(s3_bucket):
    """Even with many records on disk, list_recent is bounded."""
    # Anchor on wall-clock now so the recorded_at age filter accepts every record.
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
        seconds=failures.MAX_RECORDS_RETURNED + 10
    )
    for i in range(failures.MAX_RECORDS_RETURNED + 5):
        ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = f"{failures.FAILURE_PREFIX}{ts.replace(':', '-')}-ID{i:03d}.json"
        body = json.dumps(
            {
                "id": f"ID{i:03d}",
                "enqueued_at": ts,
                "recorded_at": ts,
                "source": "admin",
                "kind": "prompt",
                "prompt": "p",
                "reason": "r",
            }
        ).encode()
        s3.put_object(key, body, content_type="application/json")

    out = failures.list_recent()
    assert len(out) == failures.MAX_RECORDS_RETURNED


def test_queue_listing_excludes_failed_prefix(s3_bucket):
    """A breadcrumb under queue/failed/ must not appear in queue.list()."""
    from einkgen.core import queue as queue_mod

    failures.record(_make_item(item_id="FAILED-ONE"), "r")
    pending = queue_mod.enqueue("prompt", prompt="real work")

    assert [it.id for it in queue_mod.list()] == [pending.id]
    assert not queue_mod.empty()
