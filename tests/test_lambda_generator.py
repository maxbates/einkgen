"""Tests for the generator Lambda entrypoint.

The Lambda has three real triggers — the cron (every 15 min) and two
direct-invoke payloads:

- ``{"action": "render_now"}`` — render the current head.
- ``{"action": "render_item", "item_id": "..."}`` — render a specific
  pending item out of queue order (used by the per-row **Run** button
  on the SPA Queue tab).

There is no S3 ObjectCreated drain. Cron ticks (1) top up the queue
with text-LLM expansions of topics from the prompt library, then
(2) render the head.
"""

from __future__ import annotations

import pytest

from einkgen.core import queue as q
from einkgen.core.queue import QueueItem
from einkgen.lambdas import generator


CRON_EVENT = {
    "source": "aws.events",
    "detail-type": "Scheduled Event",
    "detail": {},
}

RENDER_NOW_EVENT = {"action": "render_now"}


def _patch_render_and_expand(monkeypatch, *, expansions=None):
    """Stub out pipeline + expand_topic so tests don't hit OpenAI.

    Returns ``(process_calls, expand_calls)`` for assertions.
    """
    process_calls: list[QueueItem] = []
    expand_calls: list[str] = []

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: process_calls.append(item),
    )

    # Deterministic expansion so tests can assert the queued prompt.
    queue_iter = iter(expansions or [])

    def fake_expand(topic):
        expand_calls.append(topic)
        try:
            return next(queue_iter)
        except StopIteration:
            return f"EXPANDED::{topic}"

    monkeypatch.setattr("einkgen.lambdas.generator.generate.expand_topic", fake_expand)
    return process_calls, expand_calls


def test_cron_tops_up_empty_queue_and_renders_head(monkeypatch, s3_bucket):
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)

    assert q.empty()
    generator.handler(CRON_EVENT, None)

    # Top-up filled the queue to TARGET_QUEUE_LENGTH minus the one we
    # just rendered (= TARGET - 1 remaining).
    remaining = q.list()
    assert len(remaining) == generator.TARGET_QUEUE_LENGTH - 1
    # All remaining items are cron-sourced prompt expansions.
    for it in remaining:
        assert it.kind == "prompt"
        assert it.source == "cron"
        assert it.prompt.startswith("EXPANDED::")

    # Top-up called expand_topic once per slot, render fired once.
    assert len(expand_calls) == generator.TARGET_QUEUE_LENGTH
    assert len(process_calls) == 1
    # The rendered item is the one that was at the head after top-up.
    assert process_calls[0].source == "cron"


def test_cron_does_not_top_up_when_queue_already_full(monkeypatch, s3_bucket):
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)

    # Seed the queue with TARGET_QUEUE_LENGTH items.
    seeded = [
        q.enqueue("prompt", prompt=f"seed-{i}")
        for i in range(generator.TARGET_QUEUE_LENGTH)
    ]

    generator.handler(CRON_EVENT, None)

    # No expansions fired — the queue already met the floor.
    assert expand_calls == []
    # Exactly one render happened — the head.
    assert len(process_calls) == 1
    assert process_calls[0].id == seeded[0].id
    # And the queue is now one shorter.
    assert q.count() == generator.TARGET_QUEUE_LENGTH - 1


def test_cron_partial_top_up(monkeypatch, s3_bucket):
    """A half-empty queue gets filled exactly back up to TARGET."""
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)
    # Two seed items — TARGET - 2 expansions should fire.
    q.enqueue("prompt", prompt="seed-a")
    q.enqueue("prompt", prompt="seed-b")

    generator.handler(CRON_EVENT, None)

    assert len(expand_calls) == generator.TARGET_QUEUE_LENGTH - 2
    # One render happened.
    assert len(process_calls) == 1
    # Final depth = TARGET - 1 (topped up to TARGET, then drained one).
    assert q.count() == generator.TARGET_QUEUE_LENGTH - 1


def test_cron_expand_failure_falls_back_to_raw_topic(monkeypatch, s3_bucket):
    """If expand_topic raises, we still enqueue the raw topic so the queue fills."""
    process_calls = []
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: process_calls.append(item),
    )

    def explode(_topic):
        raise RuntimeError("openai down")

    monkeypatch.setattr("einkgen.lambdas.generator.generate.expand_topic", explode)

    generator.handler(CRON_EVENT, None)

    # Queue still topped up — items hold the raw topic text (a library entry).
    remaining = q.list()
    assert len(remaining) == generator.TARGET_QUEUE_LENGTH - 1
    for it in remaining:
        assert it.kind == "prompt"
        assert it.prompt  # non-empty fallback
        assert not it.prompt.startswith("EXPANDED::")


def test_render_now_renders_head_without_topping_up(monkeypatch, s3_bucket):
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)

    head = q.enqueue("prompt", prompt="render me")
    q.enqueue("prompt", prompt="not yet")

    generator.handler(RENDER_NOW_EVENT, None)

    # Exactly the head was rendered.
    assert [c.id for c in process_calls] == [head.id]
    # And no top-up — render_now is a precise one-shot.
    assert expand_calls == []
    # The other item still pending.
    assert q.count() == 1


def test_render_now_with_empty_queue_is_noop(monkeypatch, s3_bucket):
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)
    generator.handler(RENDER_NOW_EVENT, None)
    assert process_calls == []
    assert expand_calls == []


def test_render_item_renders_specific_item_out_of_order(monkeypatch, s3_bucket):
    """``render_item`` ignores queue order — it renders the named item."""
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)

    # Two items at the head (top priority), and a target sitting at the
    # bottom. Without ``render_item``, the head would render first.
    q.enqueue("prompt", prompt="head-a", at="top")
    q.enqueue("prompt", prompt="head-b", at="top")
    target = q.enqueue("prompt", prompt="i want this one")

    generator.handler(
        {"action": "render_item", "item_id": target.id}, None
    )

    # Exactly the target item rendered — not the head — and no top-up.
    assert [c.id for c in process_calls] == [target.id]
    assert expand_calls == []
    # And the target has been popped; the heads are still there.
    remaining_ids = {it.id for it in q.list()}
    assert target.id not in remaining_ids
    assert len(remaining_ids) == 2


def test_render_item_unknown_id_is_noop(monkeypatch, s3_bucket):
    """If the id has already been drained, just log and return."""
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)
    survivor = q.enqueue("prompt", prompt="still here")

    generator.handler(
        {"action": "render_item", "item_id": "01HALREADYDRAINED"}, None
    )

    assert process_calls == []
    assert expand_calls == []
    # The unrelated item is untouched.
    assert [it.id for it in q.list()] == [survivor.id]


def test_render_item_missing_id_field_is_noop(monkeypatch, s3_bucket):
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)
    generator.handler({"action": "render_item"}, None)
    assert process_calls == []
    assert expand_calls == []


def test_unknown_event_is_ignored(monkeypatch, s3_bucket):
    """Stray S3 ObjectCreated events (post-trigger-removal) must not render."""
    process_calls, expand_calls = _patch_render_and_expand(monkeypatch)

    q.enqueue("prompt", prompt="should stay put")
    legacy_s3_event = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "s3": {"bucket": {"name": "einkgen-test"},
                       "object": {"key": "queue/whatever.json"}},
            }
        ]
    }
    generator.handler(legacy_s3_event, None)

    assert process_calls == []
    assert expand_calls == []
    assert q.count() == 1


def test_pipeline_failure_leaves_item_on_queue(monkeypatch, s3_bucket):
    """If process_item raises, finalize is not called and the item stays."""
    def explode(_item):
        raise RuntimeError("publish blew up")

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item", explode
    )
    # Stub expand so the top-up step doesn't try to hit OpenAI before
    # the render step explodes.
    monkeypatch.setattr(
        "einkgen.lambdas.generator.generate.expand_topic",
        lambda topic: f"EXP::{topic}",
    )

    q.enqueue("prompt", prompt="please render")
    before = q.count()

    with pytest.raises(RuntimeError):
        generator.handler(RENDER_NOW_EVENT, None)

    # Head still on queue.
    assert q.count() == before


# ---------------------------------------------------------------------------
# PermanentItemError handling (merged from v0.4.1.4 — adapted for the
# new render_now / render_item / cron handler shape introduced in
# v0.5.1.0; the original S3-event drain tests are gone because that
# trigger was removed).
# ---------------------------------------------------------------------------


def test_render_now_drops_permanent_failure_and_clears_head(monkeypatch, s3_bucket):
    """A PermanentItemError on the head must finalize it so the head advances.

    Regression: a prompt rejected by OpenAI's safety system
    (moderation_blocked) used to pin the head of the queue forever
    because Lambda's async-invoke retry treats every exception as
    transient. Now the generator catches PermanentItemError, drops the
    item, records a breadcrumb, and the next call drains the next item.
    """
    from einkgen.core.pipeline import PermanentItemError

    processed: list[QueueItem] = []

    def fake_process(item: QueueItem) -> None:
        if item.prompt == "blocked":
            raise PermanentItemError("safety system: moderation_blocked")
        processed.append(item)

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item", fake_process
    )

    q.enqueue("prompt", prompt="blocked")
    q.enqueue("prompt", prompt="ok")

    # First render_now drops the blocked head; second renders the survivor.
    generator.handler(RENDER_NOW_EVENT, None)
    generator.handler(RENDER_NOW_EVENT, None)

    assert [it.prompt for it in processed] == ["ok"]
    assert q.empty()


def test_permanent_error_on_cron_drops_head(monkeypatch, s3_bucket):
    """Cron path: a permanently-failing head is dropped + the tail survives."""
    from einkgen.core.pipeline import PermanentItemError

    # Stub expand_topic so the top-up phase doesn't try to call OpenAI
    # before we get to the render step. Returns a deterministic string
    # we can assert against.
    monkeypatch.setattr(
        "einkgen.lambdas.generator.generate.expand_topic",
        lambda topic: f"EXPANDED::{topic}",
    )

    def fake_process(item: QueueItem) -> None:
        # The head we seeded is the only one that should fail; cron's
        # top-up items would render successfully if they got that far.
        if item.prompt == "blocked head":
            raise PermanentItemError("safety system: moderation_blocked")

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item", fake_process
    )

    # Seed the queue at TARGET_QUEUE_LENGTH so the top-up step is a no-op.
    # The first (head) item is the one cron will try to render and drop.
    head = q.enqueue("prompt", prompt="blocked head")
    survivors = [
        q.enqueue("prompt", prompt=f"survivor-{i}")
        for i in range(generator.TARGET_QUEUE_LENGTH - 1)
    ]

    generator.handler(CRON_EVENT, None)

    # Blocked head dropped; survivors remain in FIFO order.
    remaining = q.list()
    remaining_ids = {it.id for it in remaining}
    assert head.id not in remaining_ids
    for s in survivors:
        assert s.id in remaining_ids


def test_permanent_error_writes_failure_breadcrumb(monkeypatch, s3_bucket):
    """Dropped items leave a record the Admin tab can show."""
    from einkgen.core import failures
    from einkgen.core.pipeline import PermanentItemError

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: (_ for _ in ()).throw(
            PermanentItemError("safety system: moderation_blocked")
        ),
    )

    blocked = q.enqueue("prompt", prompt="please reject me", source="admin")

    generator.handler(RENDER_NOW_EVENT, None)

    breadcrumbs = failures.list_recent()
    assert len(breadcrumbs) == 1
    rec = breadcrumbs[0]
    assert rec.id == blocked.id
    assert rec.prompt == "please reject me"
    assert rec.source == "admin"
    assert "moderation_blocked" in rec.reason
    # Queue is drained.
    assert q.empty()
