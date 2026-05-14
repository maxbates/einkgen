"""Tests for the manifest dataclass and timing helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from einkgen.core.manifest import (
    Manifest,
    compute_next_check_after,
    compute_sha256,
    iso_utc,
)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_manifest_json_round_trip():
    m = Manifest(
        version=42,
        generated_at="2026-05-13T14:00:00Z",
        image_url="https://cdn.example.com/current/image.bmp",
        image_sha256="9f1c" + "0" * 60,
        image_bytes=990123,
        display={"width": 1200, "height": 825, "levels": 8},
        next_check_after="2026-05-13T16:05:00Z",
        source={"kind": "generated", "model": "gpt-image-1", "prompt": "a foggy cliff"},
    )

    payload = m.to_json()
    parsed = Manifest.from_json(payload)
    assert parsed == m

    # Also accept bytes input.
    assert Manifest.from_json(payload.encode("utf-8")) == m


def test_manifest_from_json_allows_missing_source_subfields():
    payload = (
        '{"version":1,"generated_at":"2026-05-13T14:00:00Z",'
        '"image_url":"https://cdn.example.com/current/image.bmp",'
        '"image_sha256":"abc","image_bytes":10,'
        '"display":{"width":1200,"height":825,"levels":8},'
        '"next_check_after":"2026-05-13T16:05:00Z",'
        '"source":{"kind":"upload"}}'
    )
    m = Manifest.from_json(payload)
    assert m.source == {"kind": "upload"}


def test_compute_sha256_known_vector():
    # SHA-256 of "abc" is a well-known constant.
    assert compute_sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize(
    "now, expected",
    [
        # README example: a 10:32 wall clock rounds to 12:00 + 5m buffer.
        (_utc(2026, 5, 13, 10, 32, 0), _utc(2026, 5, 13, 12, 5, 0)),
        # Exact tick boundary: never return "now"; jump to the next tick.
        (_utc(2026, 5, 13, 10, 0, 0), _utc(2026, 5, 13, 12, 5, 0)),
        # Just before the next tick.
        (_utc(2026, 5, 13, 11, 59, 59), _utc(2026, 5, 13, 12, 5, 0)),
        # Crossing midnight.
        (_utc(2026, 5, 13, 23, 30, 0), _utc(2026, 5, 14, 0, 5, 0)),
    ],
)
def test_compute_next_check_after_2h_ticks(now, expected):
    assert compute_next_check_after(now) == expected


def test_compute_next_check_after_naive_datetime_is_treated_as_utc():
    naive = datetime(2026, 5, 13, 10, 32, 0)
    assert compute_next_check_after(naive) == _utc(2026, 5, 13, 12, 5, 0)


def test_compute_next_check_after_custom_interval():
    now = _utc(2026, 5, 13, 10, 17, 0)
    result = compute_next_check_after(
        now,
        tick_interval=timedelta(hours=1),
        buffer=timedelta(minutes=2),
    )
    assert result == _utc(2026, 5, 13, 11, 2, 0)


def test_compute_next_check_after_rejects_nonpositive_interval():
    with pytest.raises(ValueError):
        compute_next_check_after(_utc(2026, 5, 13), tick_interval=timedelta(0))


def test_iso_utc_strips_microseconds_and_appends_z():
    dt = datetime(2026, 5, 13, 14, 0, 0, 123456, tzinfo=timezone.utc)
    assert iso_utc(dt) == "2026-05-13T14:00:00Z"
