"""Tests for the generator Lambda entrypoint.

Four real triggers as of 0.6.0:

- ``aws.events`` cron — top up prompt queue to ``TARGET_PROMPT_QUEUE_LENGTH``,
  then render up to ``MAX_RENDERS_PER_TICK`` prompts into the generated
  queue buffer. No display advance.

- ``{"action": "render_one"}`` — render exactly one prompt-queue head into
  the buffer. Fired by ``/wake`` to replenish after a pop.

- ``{"action": "render_now"}`` — render the current head AND set as current
  (skips the buffer). Used by the admin **Now** button.

- ``{"action": "render_item", "item_id": "..."}`` — render a specific item
  AND set as current. Used by the per-row admin **Run** button.

There is no S3 ObjectCreated drain; cron is the only top-up trigger.
"""

from __future__ import annotations

import pytest

from einkgen.core import generated_queue as g
from einkgen.core import queue as q
from einkgen.core.queue import QueueItem
from einkgen.lambdas import generator


CRON_EVENT = {
    "source": "aws.events",
    "detail-type": "Scheduled Event",
    "detail": {},
}

RENDER_NOW_EVENT = {"action": "render_now"}
RENDER_ONE_EVENT = {"action": "render_one"}


def _patch_pipeline_and_expand(monkeypatch, *, expansions=None):
    """Stub the two pipeline entrypoints + ``expand_topic`` so tests
    don't hit OpenAI.

    Returns ``(buffer_calls, publish_calls, expand_calls)`` for
    assertions. ``buffer_item`` is the cron + ``render_one`` path;
    ``publish_item`` is the admin ``render_now`` / ``render_item``
    override. Both fakes drop a generated-queue marker for the item so
    callers can assert on buffer depth even without going through the
    real archive code.
    """
    buffer_calls: list[QueueItem] = []
    publish_calls: list[QueueItem] = []
    expand_calls: list[str] = []

    def fake_buffer(item: QueueItem) -> None:
        buffer_calls.append(item)
        g.enqueue(
            item.id,
            image_sha256="a" * 64,
            image_bytes=1,
            source={"kind": "generated", "prompt": item.prompt},
        )

    def fake_publish(item: QueueItem) -> None:
        publish_calls.append(item)

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.buffer_item", fake_buffer
    )
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.publish_item", fake_publish
    )

    # Deterministic expansion so tests can assert the queued prompt.
    queue_iter = iter(expansions or [])

    def fake_expand(topic):
        expand_calls.append(topic)
        try:
            return next(queue_iter)
        except StopIteration:
            return f"EXPANDED::{topic}"

    monkeypatch.setattr(
        "einkgen.lambdas.generator.generate.expand_topic", fake_expand
    )
    return buffer_calls, publish_calls, expand_calls


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------


def test_cron_cold_start_fills_buffer_to_target(monkeypatch, s3_bucket):
    """Cold start: cron should fill the generated buffer all the way to
    ``TARGET_GENERATED_QUEUE_LENGTH`` in a single tick, even though the
    prompt queue can't hold that many items at once.

    The buffer-refill loop tops the prompt queue back up inline whenever
    it runs dry, so a deep cold-start deficit gets filled in one go.
    The trailing ``_top_up_prompt_queue`` call leaves the prompt queue
    at its floor for SPA viewing between ticks.
    """
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)

    assert q.empty()
    assert g.empty()
    generator.handler(CRON_EVENT, None)

    # Buffer is at target.
    assert g.count() == generator.TARGET_GENERATED_QUEUE_LENGTH
    assert len(buffer_calls) == generator.TARGET_GENERATED_QUEUE_LENGTH
    # No admin-style renders happened.
    assert publish_calls == []
    # Prompt queue ends at the floor (trailing top-up after the buffer
    # loop drained it). SPA stays "non-empty pending prompts" between
    # cron ticks.
    assert q.count() == generator.TARGET_PROMPT_QUEUE_LENGTH


def test_cron_with_full_buffer_does_not_render(monkeypatch, s3_bucket):
    """When the generated buffer is already at target, cron doesn't render.

    It may still top the prompt queue up — that's cheap text-LLM cost,
    not the expensive image call we're guarding against.
    """
    buffer_calls, _, _ = _patch_pipeline_and_expand(monkeypatch)

    # Pre-fill the buffer to TARGET.
    for i in range(generator.TARGET_GENERATED_QUEUE_LENGTH):
        g.enqueue(
            f"01HFTEST{i:018d}",
            image_sha256="a" * 64,
            image_bytes=1,
            source={"kind": "generated"},
        )

    generator.handler(CRON_EVENT, None)

    assert buffer_calls == []
    # Buffer stays at exactly the target.
    assert g.count() == generator.TARGET_GENERATED_QUEUE_LENGTH


def test_cron_stops_buffering_when_prompt_queue_empties(monkeypatch, s3_bucket):
    """If only one prompt is available, cron renders one and stops cleanly."""
    buffer_calls, _, _ = _patch_pipeline_and_expand(monkeypatch)

    # Seed exactly one prompt. Prompt queue is already at TARGET? No —
    # we want to test the "prompt queue runs dry mid-buffer-render" path.
    # Pre-fill the buffer to one shy of target so cron tries to render
    # exactly once.
    for i in range(generator.TARGET_GENERATED_QUEUE_LENGTH - 1):
        g.enqueue(
            f"01HFTEST{i:018d}",
            image_sha256="a" * 64,
            image_bytes=1,
            source={"kind": "generated"},
        )

    seeded = q.enqueue("prompt", prompt="render me")

    # Suppress text-LLM top-up by pre-filling prompt queue to TARGET.
    for i in range(generator.TARGET_PROMPT_QUEUE_LENGTH - 1):
        q.enqueue("prompt", prompt=f"seed-{i}")

    generator.handler(CRON_EVENT, None)

    # Exactly one render happened (the head of the prompt queue), and
    # buffer is now at exactly TARGET.
    assert [c.id for c in buffer_calls] == [seeded.id]
    assert g.count() == generator.TARGET_GENERATED_QUEUE_LENGTH


def test_cron_expand_failure_falls_back_to_raw_topic(monkeypatch, s3_bucket):
    """expand_topic raising still results in the buffer filling.

    With ``expand_topic`` exploding we fall back to enqueueing the raw
    library topic as a prompt — buffer still fills, prompt queue still
    ends at floor.
    """
    # Use the helper but stub buffer_item with one that doesn't actually
    # enqueue a generated marker, since we want to drive the loop on
    # buffer count via the fake.
    buffer_calls, _, _ = _patch_pipeline_and_expand(monkeypatch)

    def explode(_topic):
        raise RuntimeError("openai down")

    monkeypatch.setattr("einkgen.lambdas.generator.generate.expand_topic", explode)

    generator.handler(CRON_EVENT, None)

    # Buffer reached target.
    assert g.count() == generator.TARGET_GENERATED_QUEUE_LENGTH
    assert len(buffer_calls) == generator.TARGET_GENERATED_QUEUE_LENGTH
    # Prompt queue restored to floor — items hold the raw topic text
    # because expansion failed.
    assert q.count() == generator.TARGET_PROMPT_QUEUE_LENGTH
    for it in q.list():
        assert it.kind == "prompt"
        assert it.prompt  # non-empty fallback
        assert not it.prompt.startswith("EXPANDED::")


# ---------------------------------------------------------------------------
# render_one (the /wake replenish action)
# ---------------------------------------------------------------------------


def test_render_one_buffers_head(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)
    head = q.enqueue("prompt", prompt="render me")
    q.enqueue("prompt", prompt="not yet")

    generator.handler(RENDER_ONE_EVENT, None)

    assert [c.id for c in buffer_calls] == [head.id]
    assert publish_calls == []
    assert expand_calls == []
    # Prompt queue down by one, buffer has the marker.
    assert q.count() == 1
    assert g.count() == 1


def test_render_one_with_empty_queue_is_noop(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)
    generator.handler(RENDER_ONE_EVENT, None)
    assert buffer_calls == []
    assert publish_calls == []
    assert expand_calls == []
    assert g.empty()


def test_render_one_skips_when_buffer_already_at_target(monkeypatch, s3_bucket):
    """render_one must not overshoot ``TARGET_GENERATED_QUEUE_LENGTH``.

    Wake-button mashes queue N async render_one events behind the
    in-flight invocation (reserved concurrency = 1). When they fire,
    the cap prevents N extra renders if the buffer is already at
    target.
    """
    buffer_calls, _, _ = _patch_pipeline_and_expand(monkeypatch)
    # Pre-fill the buffer to TARGET.
    for i in range(generator.TARGET_GENERATED_QUEUE_LENGTH):
        g.enqueue(
            f"01HFTEST{i:018d}",
            image_sha256="a" * 64,
            image_bytes=1,
            source={"kind": "generated"},
        )
    # Seed a prompt so the render WOULD run if the cap didn't bite.
    q.enqueue("prompt", prompt="should not render")

    generator.handler(RENDER_ONE_EVENT, None)

    # No render happened — cap held.
    assert buffer_calls == []
    # Buffer untouched.
    assert g.count() == generator.TARGET_GENERATED_QUEUE_LENGTH
    # Prompt queue untouched.
    assert q.count() == 1


# ---------------------------------------------------------------------------
# render_now / render_item (admin Now / Run — bypass the buffer)
# ---------------------------------------------------------------------------


def test_render_now_publishes_head_directly(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)
    head = q.enqueue("prompt", prompt="render me")
    q.enqueue("prompt", prompt="not yet")

    generator.handler(RENDER_NOW_EVENT, None)

    # render_now writes to current — it does NOT touch the buffer.
    assert [c.id for c in publish_calls] == [head.id]
    assert buffer_calls == []
    assert expand_calls == []
    assert q.count() == 1
    assert g.empty()


def test_render_now_with_empty_queue_is_noop(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)
    generator.handler(RENDER_NOW_EVENT, None)
    assert buffer_calls == publish_calls == []
    assert expand_calls == []


def test_render_item_publishes_specific_id(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)

    q.enqueue("prompt", prompt="head-a", at="top")
    q.enqueue("prompt", prompt="head-b", at="top")
    target = q.enqueue("prompt", prompt="i want this one")

    generator.handler(
        {"action": "render_item", "item_id": target.id}, None
    )

    assert [c.id for c in publish_calls] == [target.id]
    assert buffer_calls == []
    assert expand_calls == []
    # Target popped; heads survive.
    remaining_ids = {it.id for it in q.list()}
    assert target.id not in remaining_ids
    assert len(remaining_ids) == 2


def test_render_item_unknown_id_is_noop(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)
    survivor = q.enqueue("prompt", prompt="still here")

    generator.handler(
        {"action": "render_item", "item_id": "01HALREADYDRAINED"}, None
    )

    assert buffer_calls == publish_calls == []
    assert expand_calls == []
    assert [it.id for it in q.list()] == [survivor.id]


def test_render_item_missing_id_field_is_noop(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)
    generator.handler({"action": "render_item"}, None)
    assert buffer_calls == publish_calls == []
    assert expand_calls == []


# ---------------------------------------------------------------------------
# Stray / unknown events
# ---------------------------------------------------------------------------


def test_unknown_event_is_ignored(monkeypatch, s3_bucket):
    buffer_calls, publish_calls, expand_calls = _patch_pipeline_and_expand(monkeypatch)

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

    assert buffer_calls == publish_calls == []
    assert expand_calls == []
    assert q.count() == 1


def test_pipeline_failure_leaves_item_on_queue(monkeypatch, s3_bucket):
    def explode(_item):
        raise RuntimeError("publish blew up")

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.publish_item", explode
    )
    monkeypatch.setattr(
        "einkgen.lambdas.generator.generate.expand_topic",
        lambda topic: f"EXP::{topic}",
    )

    q.enqueue("prompt", prompt="please render")
    before = q.count()

    with pytest.raises(RuntimeError):
        generator.handler(RENDER_NOW_EVENT, None)

    assert q.count() == before


# ---------------------------------------------------------------------------
# PermanentItemError handling
# ---------------------------------------------------------------------------


def test_render_now_drops_permanent_failure_and_clears_head(monkeypatch, s3_bucket):
    from einkgen.core.pipeline import PermanentItemError

    processed: list[QueueItem] = []

    def fake_publish(item: QueueItem) -> None:
        if item.prompt == "blocked":
            raise PermanentItemError("safety system: moderation_blocked")
        processed.append(item)

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.publish_item", fake_publish
    )
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.buffer_item",
        lambda _item: None,
    )

    q.enqueue("prompt", prompt="blocked")
    q.enqueue("prompt", prompt="ok")

    generator.handler(RENDER_NOW_EVENT, None)
    generator.handler(RENDER_NOW_EVENT, None)

    assert [it.prompt for it in processed] == ["ok"]
    assert q.empty()


def test_permanent_error_on_cron_drops_head(monkeypatch, s3_bucket):
    """Cron tries to buffer the head, hits a PermanentItemError, drops it."""
    from einkgen.core.pipeline import PermanentItemError

    monkeypatch.setattr(
        "einkgen.lambdas.generator.generate.expand_topic",
        lambda topic: f"EXPANDED::{topic}",
    )

    def fake_buffer(item: QueueItem) -> None:
        if item.prompt == "blocked head":
            raise PermanentItemError("safety system: moderation_blocked")

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.buffer_item", fake_buffer
    )
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.publish_item",
        lambda _item: None,
    )

    head = q.enqueue("prompt", prompt="blocked head")

    generator.handler(CRON_EVENT, None)

    # The blocked head was dropped, freeing the queue to advance. We
    # don't assert what's left in the queue — cron does a full refill
    # this tick (top up to target, drain into buffer, top up again),
    # so the queue gets churned through entirely.
    remaining_ids = {it.id for it in q.list()}
    assert head.id not in remaining_ids


def test_permanent_error_writes_failure_breadcrumb(monkeypatch, s3_bucket):
    from einkgen.core import failures
    from einkgen.core.pipeline import PermanentItemError

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.publish_item",
        lambda item: (_ for _ in ()).throw(
            PermanentItemError("safety system: moderation_blocked")
        ),
    )
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.buffer_item",
        lambda _item: None,
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
    assert q.empty()
