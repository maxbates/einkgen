"""Tests for the generator Lambda entrypoint."""

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


def _s3_event(key: str = "queue/2026-05-13T14-00-00Z-01HFXXX.json") -> dict:
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "einkgen-test"},
                    "object": {"key": key},
                },
            }
        ]
    }


def test_cron_empty_queue_enqueues_random(monkeypatch, s3_bucket):
    process_calls: list = []

    def fake_process_item(item):
        process_calls.append(item)

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item", fake_process_item
    )

    assert q.empty()

    generator.handler(CRON_EVENT, None)

    items = q.list()
    assert len(items) == 1
    assert items[0].kind == "random"
    assert items[0].source == "cron"
    # Cron path must NOT also process — the resulting S3 event will.
    assert process_calls == []


def test_cron_nonempty_queue_processes_head(monkeypatch, s3_bucket):
    """Cron self-heals items stranded by a prior failed S3 delivery.

    Steady-state, the S3 event has already drained these. The cron only
    sees a non-empty queue when something went wrong upstream — process
    exactly one item per tick.
    """
    process_calls: list[QueueItem] = []
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: process_calls.append(item),
    )

    head = q.enqueue("prompt", prompt="stranded head")
    tail = q.enqueue("prompt", prompt="stranded tail")

    generator.handler(CRON_EVENT, None)

    # Exactly one — the head — got processed and popped.
    assert [c.id for c in process_calls] == [head.id]
    remaining = q.list()
    assert [it.id for it in remaining] == [tail.id]


def test_s3_event_pops_and_processes(monkeypatch, s3_bucket):
    process_calls: list[QueueItem] = []
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: process_calls.append(item),
    )

    enqueued = q.enqueue("prompt", prompt="render me")

    generator.handler(_s3_event(enqueued._s3_key), None)

    assert len(process_calls) == 1
    assert process_calls[0].id == enqueued.id
    assert process_calls[0].prompt == "render me"
    # And it's been popped.
    assert q.empty()


def test_s3_event_with_empty_queue_is_noop(monkeypatch, s3_bucket):
    process_calls: list = []
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: process_calls.append(item),
    )

    # The queue is empty even though we received an S3 event (e.g. someone
    # already drained it). The handler just returns.
    generator.handler(_s3_event(), None)
    assert process_calls == []


def test_permanent_error_drops_item_and_continues(monkeypatch, s3_bucket):
    """A PermanentItemError on the head must finalize it and let the drain advance.

    Regression: a prompt rejected by OpenAI's safety system (moderation_blocked)
    used to pin the head of the queue forever because Lambda's async-invoke
    retry treats every exception as transient. Now the generator catches
    PermanentItemError, drops the item, and keeps draining.
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

    blocked = q.enqueue("prompt", prompt="blocked")
    good = q.enqueue("prompt", prompt="ok")

    generator.handler(_s3_event(blocked._s3_key), None)

    # Blocked item is dropped (not in processed), good item drained.
    assert [it.prompt for it in processed] == ["ok"]
    assert q.empty()


def test_permanent_error_on_cron_drops_head(monkeypatch, s3_bucket):
    """Cron path mirrors the S3-event path: drop on PermanentItemError."""
    from einkgen.core.pipeline import PermanentItemError

    def fake_process(item: QueueItem) -> None:
        raise PermanentItemError("safety system: moderation_blocked")

    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item", fake_process
    )

    head = q.enqueue("prompt", prompt="blocked head")
    tail = q.enqueue("prompt", prompt="next attempt")

    generator.handler(CRON_EVENT, None)

    # Blocked head dropped; tail remains for the next tick / S3 event.
    remaining = q.list()
    assert [it.id for it in remaining] == [tail.id]
    assert head.id not in {it.id for it in remaining}


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

    generator.handler(_s3_event(blocked._s3_key), None)

    breadcrumbs = failures.list_recent()
    assert len(breadcrumbs) == 1
    rec = breadcrumbs[0]
    assert rec.id == blocked.id
    assert rec.prompt == "please reject me"
    assert rec.source == "admin"
    assert "moderation_blocked" in rec.reason
    # Queue is drained.
    assert q.empty()
