"""Tests for `einkgen history`."""

from __future__ import annotations

import json

from einkgen.cli import main


def _put_manifest(client, bucket: str, item_id: str, generated_at: str, source: dict):
    body = json.dumps(
        {
            "version": 1,
            "generated_at": generated_at,
            "image_url": "https://cdn.test.example.com/current/image.bmp",
            "image_sha256": "x" * 64,
            "image_bytes": 100,
            "display": {"width": 1200, "height": 825, "levels": 8},
            "next_check_after": "2026-05-13T16:05:00Z",
            "source": source,
        }
    ).encode("utf-8")
    client.put_object(
        Bucket=bucket,
        Key=f"history/{item_id}/manifest.json",
        Body=body,
        ContentType="application/json",
    )


def test_history_sorted_newest_first(s3_bucket, capsys):
    _put_manifest(
        s3_bucket, "einkgen-test", "id-old",
        "2026-05-13T10:00:00Z", {"kind": "generated", "prompt": "old cliff"},
    )
    _put_manifest(
        s3_bucket, "einkgen-test", "id-mid",
        "2026-05-13T12:00:00Z", {"kind": "upload", "image_s3_key": "queue/staged/x.jpg"},
    )
    _put_manifest(
        s3_bucket, "einkgen-test", "id-new",
        "2026-05-13T14:00:00Z", {"kind": "generated", "prompt": "new cliff"},
    )

    rc = main(["history"])
    out = capsys.readouterr().out

    assert rc == 0
    lines = [ln for ln in out.strip().splitlines() if ln]
    assert len(lines) == 3

    # Newest first.
    assert lines[0].startswith("2026-05-13T14:00:00Z")
    assert "id-new" in lines[0]
    assert "generated" in lines[0]
    assert "new cliff" in lines[0]

    assert lines[1].startswith("2026-05-13T12:00:00Z")
    assert "id-mid" in lines[1]
    assert "upload" in lines[1]
    assert "queue/staged/x.jpg" in lines[1]

    assert lines[2].startswith("2026-05-13T10:00:00Z")
    assert "id-old" in lines[2]


def test_history_respects_limit(s3_bucket, capsys):
    for i in range(5):
        _put_manifest(
            s3_bucket, "einkgen-test", f"id-{i}",
            f"2026-05-13T{10 + i:02d}:00:00Z", {"kind": "generated", "prompt": f"p{i}"},
        )

    rc = main(["history", "--limit", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [ln for ln in out.strip().splitlines() if ln]
    assert len(lines) == 2
    # Newest two.
    assert "id-4" in lines[0]
    assert "id-3" in lines[1]


def test_history_truncates_long_prompt(s3_bucket, capsys):
    long_prompt = "x" * 200
    _put_manifest(
        s3_bucket, "einkgen-test", "id-long",
        "2026-05-13T14:00:00Z", {"kind": "generated", "prompt": long_prompt},
    )
    rc = main(["history"])
    out = capsys.readouterr().out
    assert rc == 0
    # The full 200-char prompt should not appear verbatim — it's truncated to 60.
    assert long_prompt not in out
    assert "x" * 50 in out  # but enough of it does survive


def test_history_empty(s3_bucket, capsys):
    rc = main(["history"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No history entries found." in out
