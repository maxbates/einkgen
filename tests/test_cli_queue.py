"""Tests for `einkgen queue {ls,rm,prompt,image}`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from einkgen.cli import main
from einkgen.cli import queue as cli_queue
from einkgen.core import queue as queue_core
from tests.conftest import TEST_BUCKET


@pytest.fixture
def lambda_mock(monkeypatch):
    """Patch boto3.client('lambda') so --now doesn't hit AWS."""
    client = MagicMock()
    monkeypatch.setattr(cli_queue, "boto3", MagicMock(client=lambda _: client))
    return client


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


def test_prompt_top_flag_jumps_queue(s3_bucket, capsys):
    main(["queue", "prompt", "seed"])
    capsys.readouterr()
    main(["queue", "prompt", "urgent", "--top"])
    capsys.readouterr()

    ids_in_order = [it.id for it in queue_core.list()]
    # The --top item is now the head.
    assert queue_core.list()[0].prompt == "urgent"
    assert queue_core.list()[1].prompt == "seed"


def test_image_top_flag_jumps_queue(s3_bucket, tmp_path, capsys):
    src = tmp_path / "x.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"fake")
    main(["queue", "prompt", "seed"])
    capsys.readouterr()
    main(["queue", "image", str(src), "--top"])
    capsys.readouterr()

    assert queue_core.list()[0].kind == "image"
    assert queue_core.list()[1].prompt == "seed"


def test_image_with_prompt_records_restyle_hint(s3_bucket, tmp_path, capsys):
    src = tmp_path / "skyline.jpg"
    src.write_bytes(b"\xff\xd8\xff" + b"fake-jpeg-bytes" * 8)

    rc = main(["queue", "image", str(src), "--prompt", "render as a woodcut"])
    assert rc == 0
    items = queue_core.list()
    assert len(items) == 1
    assert items[0].kind == "image"
    assert items[0].prompt == "render as a woodcut"
    assert items[0].image_s3_key.startswith("queue/staged/")


def test_image_missing_path_exits_nonzero(s3_bucket, tmp_path, capsys):
    missing = tmp_path / "nope.jpg"
    rc = main(["queue", "image", str(missing)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not a file" in err
    # No queue items written.
    assert queue_core.list() == []


# --------------------------------------------------------------------------
# --now flag
# --------------------------------------------------------------------------


def test_prompt_now_flag_enqueues_top_and_invokes_generator(
    s3_bucket, lambda_mock, capsys
):
    rc = main(["queue", "prompt", "render this now please", "--now"])
    assert rc == 0
    capsys.readouterr()

    items = queue_core.list()
    assert len(items) == 1
    # --now implies top placement (priority "0" prefix on the S3 key).
    assert items[0]._s3_key.startswith("queue/0-")

    # Lambda was async-invoked with render_now.
    lambda_mock.invoke.assert_called_once()
    kwargs = lambda_mock.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "einkgen-generator"
    assert kwargs["InvocationType"] == "Event"
    assert json.loads(kwargs["Payload"].decode()) == {"action": "render_now"}


def test_image_now_flag_enqueues_top_and_invokes_generator(
    s3_bucket, lambda_mock, tmp_path, capsys
):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"\xff\xd8\xfffake")

    rc = main(["queue", "image", str(src), "--now"])
    assert rc == 0
    capsys.readouterr()

    items = queue_core.list()
    assert len(items) == 1
    assert items[0].kind == "image"
    assert items[0]._s3_key.startswith("queue/0-")

    lambda_mock.invoke.assert_called_once()
    payload = json.loads(lambda_mock.invoke.call_args.kwargs["Payload"].decode())
    assert payload == {"action": "render_now"}


def test_prompt_top_flag_does_not_invoke_generator(s3_bucket, lambda_mock, capsys):
    """--top is placement only; the generator must NOT be invoked."""
    rc = main(["queue", "prompt", "soon-ish", "--top"])
    assert rc == 0
    capsys.readouterr()
    assert lambda_mock.invoke.call_count == 0


def test_now_and_top_are_mutually_exclusive(s3_bucket, lambda_mock, capsys):
    """argparse must reject combining --top with --now."""
    with pytest.raises(SystemExit):
        main(["queue", "prompt", "hi", "--top", "--now"])


def test_prompt_now_uses_custom_generator_name_from_env(
    s3_bucket, lambda_mock, monkeypatch, capsys
):
    monkeypatch.setenv("EINKGEN_GENERATOR_FUNCTION_NAME", "einkgen-generator-dev")
    main(["queue", "prompt", "x", "--now"])
    capsys.readouterr()
    assert lambda_mock.invoke.call_args.kwargs["FunctionName"] == "einkgen-generator-dev"
