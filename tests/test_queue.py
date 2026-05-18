"""Tests for the two-priority S3-prefix queue."""

from __future__ import annotations

import json
import time

import pytest

from einkgen.core import queue as q
from tests.conftest import TEST_BUCKET


def test_enqueue_writes_under_queue_prefix_with_priority(s3_bucket):
    item = q.enqueue("prompt", prompt="hello")

    assert item.kind == "prompt"
    assert item.prompt == "hello"
    assert item.image_s3_key is None
    assert item._s3_key is not None
    # Default ``at="bottom"`` → priority "1" in the key.
    assert item._s3_key.startswith(f"queue/{q.PRIORITY_BOTTOM}-")
    assert not item._s3_key.startswith("queue/staged/")

    obj = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=item._s3_key)
    payload = json.loads(obj["Body"].read())
    assert payload["id"] == item.id
    assert payload["kind"] == "prompt"
    assert payload["prompt"] == "hello"
    assert payload["source"] == "cli"
    assert payload["enqueued_at"] == item.enqueued_at
    # The JSON body must NOT carry a position field — order is in the key.
    assert "position" not in payload
    assert "_s3_key" not in payload


def test_enqueue_at_top_uses_top_priority_key(s3_bucket):
    item = q.enqueue("prompt", prompt="urgent", at="top")
    assert item._s3_key is not None
    assert item._s3_key.startswith(f"queue/{q.PRIORITY_TOP}-")


def test_enqueue_validates_kind_fields(s3_bucket):
    with pytest.raises(ValueError):
        q.enqueue("prompt")  # missing prompt
    with pytest.raises(ValueError):
        q.enqueue("prompt", prompt="hi", image_s3_key="queue/staged/x")
    with pytest.raises(ValueError):
        q.enqueue("image")  # missing image_s3_key
    with pytest.raises(ValueError):
        q.enqueue("random", prompt="hi")
    with pytest.raises(ValueError):
        q.enqueue("weird")


def test_enqueue_rejects_unknown_at(s3_bucket):
    with pytest.raises(ValueError):
        q.enqueue("prompt", prompt="x", at="middle")


def test_enqueue_image_with_prompt_is_allowed(s3_bucket):
    """kind='image' may carry an optional prompt that restyles the upload."""
    item = q.enqueue("image", image_s3_key="queue/staged/x.jpg", prompt="watercolor")
    assert item.kind == "image"
    assert item.image_s3_key == "queue/staged/x.jpg"
    assert item.prompt == "watercolor"

    payload = json.loads(s3_bucket.get_object(Bucket=TEST_BUCKET, Key=item._s3_key)["Body"].read())
    assert payload["prompt"] == "watercolor"
    assert payload["image_s3_key"] == "queue/staged/x.jpg"


def test_list_returns_bottom_items_in_fifo_order(s3_bucket):
    a = q.enqueue("prompt", prompt="first")
    time.sleep(0.005)
    b = q.enqueue("prompt", prompt="second")
    time.sleep(0.005)
    c = q.enqueue("random", source="cron")

    items = q.list()
    ids = [it.id for it in items]
    assert ids == [a.id, b.id, c.id]
    assert items[2].source == "cron"


def test_top_priority_drains_before_bottom(s3_bucket):
    a = q.enqueue("prompt", prompt="first")  # bottom
    b = q.enqueue("prompt", prompt="second")  # bottom
    jumper = q.enqueue("prompt", prompt="urgent", at="top")

    # Top item always lands at the head, even if it was enqueued last.
    ids = [it.id for it in q.list()]
    assert ids == [jumper.id, a.id, b.id]
    head = q.peek_head()
    assert head is not None and head.id == jumper.id


def test_multiple_top_inserts_are_fifo_within_top_queue(s3_bucket):
    a = q.enqueue("prompt", prompt="first")  # bottom
    time.sleep(0.005)
    t1 = q.enqueue("prompt", prompt="urgent-1", at="top")
    time.sleep(0.005)
    t2 = q.enqueue("prompt", prompt="urgent-2", at="top")

    # Within the top queue, the older insert wins (FIFO). The bottom
    # item drains last.
    assert [it.id for it in q.list()] == [t1.id, t2.id, a.id]


def test_peek_head_returns_oldest_top_and_does_not_delete(s3_bucket):
    bottom = q.enqueue("prompt", prompt="bottom")
    time.sleep(0.005)
    top1 = q.enqueue("prompt", prompt="top-1", at="top")
    time.sleep(0.005)
    q.enqueue("prompt", prompt="top-2", at="top")

    head = q.peek_head()
    assert head is not None
    assert head.id == top1.id

    # Peek must not consume — all three items remain on the queue.
    assert len(q.list()) == 3
    # And the bottom item is still pending at the tail.
    assert q.list()[-1].id == bottom.id


def test_get_returns_item_by_id(s3_bucket):
    q.enqueue("prompt", prompt="first")
    target = q.enqueue("prompt", prompt="second", at="top")

    fetched = q.get(target.id)
    assert fetched is not None
    assert fetched.id == target.id
    assert fetched.prompt == "second"
    assert q.get("01HNONEXIST") is None


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
    q.enqueue("prompt", prompt="first")
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


def test_cancel_works_across_priorities(s3_bucket):
    """Cancel must find an item regardless of which priority queue it's in."""
    top = q.enqueue("prompt", prompt="top", at="top")
    bot = q.enqueue("prompt", prompt="bot", at="bottom")

    assert q.cancel(top.id) is True
    assert [it.id for it in q.list()] == [bot.id]
    assert q.cancel(bot.id) is True
    assert q.list() == []


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


def test_count_matches_list_length(s3_bucket):
    assert q.count() == 0
    q.enqueue("prompt", prompt="a")
    q.enqueue("prompt", prompt="b", at="top")
    assert q.count() == 2
    assert q.count() == len(q.list())


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


def test_legacy_items_without_priority_prefix_sort_after_both_queues(s3_bucket):
    """Items written before the priority-prefixed key format must still load.

    Their keys look like ``queue/<iso_ts>-<ulid>.json`` (no priority
    prefix). The first character of the timestamp is a digit (year), and
    ``"2"`` > ``"1"`` > ``"0"`` lex-wise, so legacy items sort *after*
    both new priorities — they drain as the tail of the queue, no
    migration needed.
    """
    # Hand-write a legacy-shaped JSON without a priority prefix.
    legacy_key = "queue/2026-05-13T14-00-00Z-LEGACY01.json"
    s3_bucket.put_object(
        Bucket=TEST_BUCKET,
        Key=legacy_key,
        Body=json.dumps(
            {
                "id": "LEGACY01",
                "enqueued_at": "2026-05-13T14:00:00Z",
                "source": "cli",
                "kind": "prompt",
                "prompt": "old item",
            }
        ).encode("utf-8"),
    )
    new_top = q.enqueue("prompt", prompt="new top", at="top")
    new_bottom = q.enqueue("prompt", prompt="new bottom", at="bottom")

    ids = [it.id for it in q.list()]
    # new_top first (priority "0"), new_bottom second (priority "1"),
    # legacy last (prefix "2026..." > "1-...").
    assert ids == [new_top.id, new_bottom.id, "LEGACY01"]
