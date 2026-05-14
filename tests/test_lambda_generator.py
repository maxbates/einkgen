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


def test_cron_nonempty_queue_is_noop(monkeypatch, s3_bucket):
    process_calls: list = []
    monkeypatch.setattr(
        "einkgen.lambdas.generator.pipeline.process_item",
        lambda item: process_calls.append(item),
    )

    existing = q.enqueue("prompt", prompt="already pending")

    generator.handler(CRON_EVENT, None)

    # Same one item, nothing new.
    items = q.list()
    assert [it.id for it in items] == [existing.id]
    assert process_calls == []


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
