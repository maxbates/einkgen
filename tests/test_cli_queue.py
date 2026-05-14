"""Tests for `einkgen queue {ls,rm,prompt,image}`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from einkgen.cli import main
from einkgen.core import queue as queue_core
from tests.conftest import TEST_BUCKET


def test_ls_empty_prints_marker(s3_bucket, capsys):
    rc = main(["queue", "ls"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "queue empty" in out.lower()


def test_prompt_enqueues_and_prints_id(s3_bucket, capsys):
    rc = main(["queue", "prompt", "a foggy cliff at dawn"])
    out = capsys.readouterr().out.strip()
    assert rc == 0

    items = queue_core.list()
    assert len(items) == 1
    assert items[0].prompt == "a foggy cliff at dawn"
    assert items[0].kind == "prompt"
    assert items[0].source == "cli"
    # The printed line is the new item's id.
    assert out == items[0].id


def test_ls_shows_pending_items(s3_bucket, capsys):
    main(["queue", "prompt", "hello"])
    capsys.readouterr()  # discard
    main(["queue", "prompt", "world"])
    capsys.readouterr()

    rc = main(["queue", "ls"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "hello" in out
    assert "world" in out
    assert "prompt" in out


def test_rm_cancels_existing_item(s3_bucket, capsys):
    main(["queue", "prompt", "first"])
    capsys.readouterr()
    first_id = queue_core.list()[0].id

    rc = main(["queue", "rm", first_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cancelled" in out
    assert queue_core.list() == []


def test_rm_unknown_id_exits_nonzero(s3_bucket, capsys):
    rc = main(["queue", "rm", "01HF7ZBOGUS"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no pending item" in err


def test_rm_does_not_substring_match(s3_bucket, capsys):
    """The CLI must not accidentally cancel a sibling item.

    Before the suffix-match fix, ``cancel(<single-char>)`` would delete
    any queue object whose key contained that character.
    """
    main(["queue", "prompt", "real"])
    capsys.readouterr()
    real_id = queue_core.list()[0].id

    # Try to cancel with a 1-char prefix that's almost certainly inside
    # the ULID — must fail and leave the real item intact.
    rc = main(["queue", "rm", real_id[:1]])
    assert rc == 1
    assert queue_core.list()[0].id == real_id


def test_image_uploads_staged_and_enqueues(s3_bucket, tmp_path, capsys):
    src = tmp_path / "cat.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"fake-jpeg-bytes" * 8)

    rc = main(["queue", "image", str(src)])
    out = capsys.readouterr().out.strip()
    assert rc == 0

    items = queue_core.list()
    assert len(items) == 1
    assert items[0].kind == "image"
    assert items[0].image_s3_key.startswith("queue/staged/")
    expected_sha8 = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
    assert items[0].image_s3_key == f"queue/staged/{expected_sha8}-cat.jpg"
    assert out == items[0].id

    # The staged object actually landed in S3 with the file's bytes.
    staged = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=items[0].image_s3_key)
    assert staged["Body"].read() == src.read_bytes()


def test_image_missing_path_exits_nonzero(s3_bucket, tmp_path, capsys):
    missing = tmp_path / "nope.jpg"
    rc = main(["queue", "image", str(missing)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not a file" in err
    # No queue items written.
    assert queue_core.list() == []
