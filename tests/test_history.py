"""Tests for ``core.history.recent_prompts`` — the avoid-list reader."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from einkgen.core import history, s3

from .conftest import TEST_BUCKET


def _write_history(item_id: str, prompt: str | None, generated_at: str) -> None:
    """Stamp a minimal ``history/<id>/manifest.json`` into the test bucket.

    Only the fields ``recent_prompts`` reads (``source.prompt``,
    ``generated_at`` for ordering) are populated — enough to exercise
    the read path without dragging the full Manifest dataclass in.
    """
    body = {
        "version": 1,
        "generated_at": generated_at,
        "image_url": "https://example/x.bmp",
        "image_sha256": "a" * 64,
        "image_bytes": 1,
        "display": {},
        "next_check_after": generated_at,
        "source": {"kind": "generated"} if prompt is None else {
            "kind": "generated", "prompt": prompt,
        },
    }
    s3.put_object(
        f"history/{item_id}/manifest.json",
        json.dumps(body).encode("utf-8"),
        content_type="application/json",
    )


def _iso(year: int, month: int, day: int) -> str:
    return datetime(year, month, day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recent_prompts_returns_newest_first(s3_bucket):
    # ULID-ish ids — lex order is time-monotonic so newest sorts last.
    _write_history("01HFA000000000000000000001", "first frame", _iso(2026, 1, 1))
    _write_history("01HFB000000000000000000002", "second frame", _iso(2026, 2, 1))
    _write_history("01HFC000000000000000000003", "third frame", _iso(2026, 3, 1))

    out = history.recent_prompts()

    assert out == ["third frame", "second frame", "first frame"]


def test_recent_prompts_respects_limit(s3_bucket):
    for i in range(5):
        _write_history(
            f"01HF{i:022d}", f"prompt-{i}", _iso(2026, 1, i + 1),
        )

    out = history.recent_prompts(limit=2)

    assert len(out) == 2
    assert out == ["prompt-4", "prompt-3"]


def test_recent_prompts_skips_items_without_prompt(s3_bucket):
    """Image-only submissions (no source.prompt) are silently dropped."""
    _write_history("01HFA000000000000000000001", "text prompt", _iso(2026, 1, 1))
    _write_history("01HFB000000000000000000002", None, _iso(2026, 2, 1))
    _write_history("01HFC000000000000000000003", "another text", _iso(2026, 3, 1))

    out = history.recent_prompts()

    assert out == ["another text", "text prompt"]


def test_recent_prompts_skips_unreadable_manifest(s3_bucket, caplog):
    """A garbage manifest logs + skips rather than taking the cron down."""
    _write_history("01HFA000000000000000000001", "good one", _iso(2026, 1, 1))
    # Write a malformed JSON body for the newer id.
    s3.put_object(
        "history/01HFB000000000000000000002/manifest.json",
        b"{ not valid json",
        content_type="application/json",
    )

    out = history.recent_prompts()

    assert out == ["good one"]


def test_recent_prompts_with_empty_bucket_returns_empty(s3_bucket):
    assert history.recent_prompts() == []


def test_recent_prompts_zero_limit_short_circuits(s3_bucket):
    _write_history("01HFA000000000000000000001", "anything", _iso(2026, 1, 1))
    assert history.recent_prompts(limit=0) == []


def test_recent_prompts_uses_bucket_from_env(s3_bucket):
    """Cheap belt-and-braces — the test fixture sets EINKGEN_BUCKET."""
    import os

    assert os.environ["EINKGEN_BUCKET"] == TEST_BUCKET
