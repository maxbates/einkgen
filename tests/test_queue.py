"""Tests for the S3-prefix-backed FIFO queue."""

from __future__ import annotations

import json
import time

import pytest

from einkgen.core import queue as q
from tests.conftest import TEST_BUCKET


def test_enqueue_writes_under_queue_prefix(s3_bucket):
    item = q.enqueue("prompt", prompt="hello")

    assert item.kind == "prompt"
    assert item.prompt == "hello"
    assert item.image_s3_key is None
    assert item._s3_key is not None
    assert item._s3_key.startswith("queue/")
    assert not item._s3_key.startswith("queue/staged/")

    obj = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=item._s3_key)
    payload = json.loads(obj["Body"].read())
    assert payload["id"] == item.id
    assert payload["kind"] == "prompt"
    assert payload["prompt"] == "hello"
    assert payload["source"] == "cli"
    assert payload["enqueued_at"] == item.enqueued_at
    assert "_s3_key" not in payload  # internal field stays out of the JSON


def test_enqueue_validates_kind_fields(s3_bucket):
    with pytest.raises(ValueError):
        q.enqueue("prompt")  # missing prompt
    with pytest.raises(ValueError):
        q.enqueue("prompt", prompt="hi", image_s3_key="queue/staged/x")
    with pytest.raises(ValueError):
        q.enqueue("image")  # missing image_s3_key
    with pytest.raises(ValueError):
        q.enqueue("image", image_s3_key="queue/staged/x", prompt="hi")
    with pytest.raises(ValueError):
        q.enqueue("random", prompt="hi")
    with pytest.raises(ValueError):
        q.enqueue("weird")


def test_list_returns_items_in_fifo_order(s3_bucket):
    a = q.enqueue("prompt", prompt="first")
    time.sleep(0.005)
    b = q.enqueue("prompt", prompt="second")
    time.sleep(0.005)
    c = q.enqueue("random", source="cron")

    items = q.list()
    ids = [it.id for it in items]
    assert ids == [a.id, b.id, c.id]
    assert items[2].source == "cron"


def test_peek_head_returns_oldest_and_does_not_delete(s3_bucket):
    a = q.enqueue("prompt", prompt="first")
    time.sleep(0.005)
    q.enqueue("prompt", prompt="second")
    time.sleep(0.005)
    q.enqueue("prompt", prompt="third")

    head = q.peek_head()
    assert head is not None
    assert head.id == a.id

    # Peek must not consume — all three items remain on the queue.
    assert len(q.list()) == 3


def test_finalize_deletes_the_item(s3_bucket):
    a = q.enqueue("prompt", prompt="first")
    q.enqueue("prompt", prompt="second")

    head = q.peek_head()
    assert head is not None and head.id == a.id
    q.finalize(head)

    remaining = q.list()
    assert len(remaining) == 1
    assert remaining[0].id != a.id


def test_finalize_is_idempotent(s3_bucket):
    a = q.enqueue("prompt", prompt="first")
    head = q.peek_head()
    assert head is not None
    q.finalize(head)
    # Second call is a no-op (key is gone); must not raise.
    q.finalize(head)
    assert q.empty() is True


def test_peek_head_returns_none_on_empty(s3_bucket):
    assert q.peek_head() is None


def test_cancel_deletes_by_id(s3_bucket):
    q.enqueue("prompt", prompt="first")
    target = q.enqueue("prompt", prompt="second")
    q.enqueue("prompt", prompt="third")

    assert q.cancel(target.id) is True
    assert q.cancel("bogus-id-does-not-exist") is False

    remaining_ids = [it.id for it in q.list()]
    assert target.id not in remaining_ids
    assert len(remaining_ids) == 2


def test_cancel_does_not_match_substrings(s3_bucket):
    """Guard against the prior substring-match bug.

    Passing a short prefix that happens to appear inside another item's
    id must NOT delete that other item.
    """
    real = q.enqueue("prompt", prompt="real")
    # ULIDs are 26 chars; the first character is almost always shared
    # across items enqueued in the same era. Passing a 1-char prefix used
    # to match every key under queue/.
    assert q.cancel(real.id[:1]) is False
    # And the real item is still there.
    assert any(it.id == real.id for it in q.list())


def test_empty(s3_bucket):
    assert q.empty() is True
    item = q.enqueue("prompt", prompt="hi")
    assert q.empty() is False
    head = q.peek_head()
    assert head is not None and head.id == item.id
    q.finalize(head)
    assert q.empty() is True


def test_peek_head_ignores_staged_objects(s3_bucket):
    # Drop a staged object directly into queue/staged/ — this is what the CLI
    # does before enqueueing an image item.
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key="queue/staged/abc123-cat.jpg",
        Body=b"fake-jpeg-bytes",
    )

    assert q.empty() is True
    assert q.list() == []
    assert q.peek_head() is None

    # The staged object still exists; it just isn't part of the queue view.
    head = s3_bucket.head_object(Bucket=TEST_BUCKET, Key="queue/staged/abc123-cat.jpg")
    assert head["ContentLength"] == len(b"fake-jpeg-bytes")
