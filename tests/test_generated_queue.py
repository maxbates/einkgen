"""Tests for the FIFO generated queue (``core/generated_queue.py``)."""

from __future__ import annotations

import json

from einkgen.core import generated_queue as g


def _enqueue(history_id: str, *, prompt: str = "subject") -> g.GeneratedItem:
    return g.enqueue(
        history_id,
        image_sha256="a" * 64,
        image_bytes=42,
        source={"kind": "generated", "model": "gpt-image-2", "prompt": prompt},
    )


def test_empty_buffer_is_idempotent(s3_bucket):
    assert g.empty()
    assert g.count() == 0
    assert g.list() == []
    assert g.peek_head() is None
    assert g.get("01HABSENTID") is None
    assert g.cancel("01HABSENTID") is False


def test_enqueue_appends_to_fifo_order(s3_bucket):
    a = _enqueue("01HAAA00000000000000000000")
    b = _enqueue("01HBBB00000000000000000000")
    c = _enqueue("01HCCC00000000000000000000")

    items = g.list()
    assert [it.history_id for it in items] == [a.history_id, b.history_id, c.history_id]
    assert g.count() == 3
    head = g.peek_head()
    assert head is not None
    assert head.history_id == a.history_id


def test_get_matches_by_history_id_suffix(s3_bucket):
    a = _enqueue("01HAAA00000000000000000000")
    b = _enqueue("01HBBB00000000000000000000")

    found = g.get(b.history_id)
    assert found is not None
    assert found.history_id == b.history_id
    assert found.source["prompt"] == "subject"
    # A bogus id doesn't match.
    assert g.get("01HZZZNONE") is None
    # Order still intact.
    assert [it.history_id for it in g.list()] == [a.history_id, b.history_id]


def test_finalize_deletes_only_target(s3_bucket):
    a = _enqueue("01HAAA00000000000000000000")
    b = _enqueue("01HBBB00000000000000000000")

    head = g.peek_head()
    assert head is not None
    g.finalize(head)

    remaining = g.list()
    assert [it.history_id for it in remaining] == [b.history_id]
    # finalize again on the same item is harmless after delete (no error).


def test_cancel_returns_true_only_when_deleted(s3_bucket):
    _enqueue("01HAAA00000000000000000000")
    _enqueue("01HBBB00000000000000000000")

    assert g.cancel("01HABSENTID") is False
    assert g.cancel("01HAAA00000000000000000000") is True
    assert g.cancel("01HAAA00000000000000000000") is False
    assert g.count() == 1


def test_marker_body_round_trips_metadata(s3_bucket):
    item = g.enqueue(
        "01HCCC00000000000000000000",
        image_sha256="b" * 64,
        image_bytes=99,
        source={"kind": "uploaded", "prompt": "phone photo"},
    )
    # Inspect the stored bytes — these are what the SPA's GET /generated
    # ultimately surfaces.
    assert item._s3_key is not None
    from einkgen.core import s3

    payload = json.loads(s3.get_object(item._s3_key))
    assert payload["history_id"] == "01HCCC00000000000000000000"
    assert payload["image_sha256"] == "b" * 64
    assert payload["image_bytes"] == 99
    assert payload["source"] == {"kind": "uploaded", "prompt": "phone photo"}
    assert payload["queued_at"]
    assert "_s3_key" not in payload


def test_enqueue_requires_history_id(s3_bucket):
    import pytest

    with pytest.raises(ValueError):
        g.enqueue("", image_sha256="a" * 64, image_bytes=1)
