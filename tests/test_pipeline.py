"""Tests for ``core/pipeline.py``.

The pipeline lazy-imports ``einkgen.core.{generate,convert,publish}``. Those
modules are owned by other tracks, so we install fake modules in ``sys.modules``
before importing the pipeline.
"""

from __future__ import annotations

import sys
import types

import pytest

from einkgen.core import pipeline
from einkgen.core.queue import QueueItem
from tests.conftest import TEST_BUCKET


def _install_fake_modules(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, list] = {
        "generate": [],
        "generate_from_image": [],
        "random_prompt": [],
        "convert": [],
        "publish": [],
    }

    def fake_generate(prompt):
        calls["generate"].append(prompt)
        return b"fake-png-bytes-for-" + (prompt or "").encode()

    def fake_generate_from_image(prompt, image_bytes, *, image_filename="input.png"):
        calls["generate_from_image"].append(
            {"prompt": prompt, "image": image_bytes, "filename": image_filename}
        )
        return b"restyled-png-from-" + (prompt or "").encode()

    def fake_random_prompt():
        calls["random_prompt"].append(True)
        return "Geometric composition: overlapping circles"

    def fake_convert(png, *, is_generated=False):
        calls["convert"].append({"png": png, "is_generated": is_generated})
        return b"fake-bmp-" + png[:16]

    def fake_publish(processed_bmp, *, source, item_id, original, prompt):
        calls["publish"].append(
            {
                "processed_bmp": processed_bmp,
                "source": source,
                "item_id": item_id,
                "original": original,
                "prompt": prompt,
            }
        )

    generate_mod = types.SimpleNamespace(
        generate=fake_generate,
        generate_from_image=fake_generate_from_image,
        random_prompt=fake_random_prompt,
        BASE_PROMPT="",
        PROMPT_LIBRARY=[],
        MODEL="gpt-image-2",
    )
    convert_mod = types.SimpleNamespace(convert=fake_convert)
    publish_mod = types.SimpleNamespace(publish=fake_publish)

    monkeypatch.setitem(sys.modules, "einkgen.core.generate", generate_mod)
    monkeypatch.setitem(sys.modules, "einkgen.core.convert", convert_mod)
    monkeypatch.setitem(sys.modules, "einkgen.core.publish", publish_mod)

    return calls


def test_prompt_kind_runs_generate_convert_publish(monkeypatch):
    calls = _install_fake_modules(monkeypatch)

    item = QueueItem(
        id="01HFTEST0001",
        enqueued_at="2026-05-13T14:00:00Z",
        source="cli",
        kind="prompt",
        prompt="a foggy cliff at dawn",
    )

    pipeline.process_item(item)

    assert calls["generate"] == ["a foggy cliff at dawn"]
    assert len(calls["convert"]) == 1
    assert calls["convert"][0]["png"].startswith(b"fake-png-bytes-for-")
    # Prompt-kind output is a `gpt-image-2` 1200x832 composed for the whole
    # canvas — convert can center-crop the 7-pixel height overflow without
    # resampling.
    assert calls["convert"][0]["is_generated"] is True
    assert len(calls["publish"]) == 1
    p = calls["publish"][0]
    assert p["item_id"] == "01HFTEST0001"
    assert p["prompt"] == "a foggy cliff at dawn"
    assert p["source"] == {
        "kind": "generated",
        "model": "gpt-image-2",
        "prompt": "a foggy cliff at dawn",
    }
    assert p["processed_bmp"].startswith(b"fake-bmp-")
    assert p["original"].startswith(b"fake-png-bytes-for-")


def test_image_kind_fetches_from_s3(monkeypatch, s3_bucket):
    calls = _install_fake_modules(monkeypatch)

    staged_key = "queue/staged/deadbeef-cat.jpg"
    s3_bucket.put_object(Bucket=TEST_BUCKET, Key=staged_key, Body=b"real-jpeg-bytes")

    item = QueueItem(
        id="01HFTEST0002",
        enqueued_at="2026-05-13T14:00:00Z",
        source="cli",
        kind="image",
        image_s3_key=staged_key,
    )

    pipeline.process_item(item)

    # No call to generate for image kind.
    assert calls["generate"] == []
    assert calls["random_prompt"] == []
    # Convert sees the bytes we fetched from S3. Raw uploads pass through
    # without is_generated so convert scale-fits arbitrary dimensions.
    assert calls["convert"] == [{"png": b"real-jpeg-bytes", "is_generated": False}]
    p = calls["publish"][0]
    assert p["item_id"] == "01HFTEST0002"
    assert p["original"] == b"real-jpeg-bytes"
    # ARCHITECTURE §7: model/prompt are omitted for image-kind uploads,
    # not present-with-null.
    assert p["source"] == {"kind": "uploaded"}
    assert "model" not in p["source"]
    assert "prompt" not in p["source"]
    assert p["prompt"] is None

    # Staged source must have been cleaned up after successful publish.
    assert "Contents" not in s3_bucket.list_objects_v2(
        Bucket=TEST_BUCKET, Prefix=staged_key
    )


def test_image_kind_with_prompt_calls_generate_from_image(monkeypatch, s3_bucket):
    """image + prompt: restyle upload via gpt-image-2 edit, then convert."""
    calls = _install_fake_modules(monkeypatch)

    staged_key = "queue/staged/abc12345-skyline.jpg"
    s3_bucket.put_object(Bucket=TEST_BUCKET, Key=staged_key, Body=b"real-jpeg-bytes")

    item = QueueItem(
        id="01HFTEST0006",
        enqueued_at="2026-05-13T14:00:00Z",
        source="email",
        kind="image",
        image_s3_key=staged_key,
        prompt="render as a woodcut",
    )

    pipeline.process_item(item)

    # generate (text-to-image) not called; edit endpoint was.
    assert calls["generate"] == []
    assert len(calls["generate_from_image"]) == 1
    edit_call = calls["generate_from_image"][0]
    assert edit_call["prompt"] == "render as a woodcut"
    assert edit_call["image"] == b"real-jpeg-bytes"
    assert edit_call["filename"] == "abc12345-skyline.jpg"

    # Convert sees the restyled bytes, not the original upload. Restyled
    # output is a generated frame so is_generated=True.
    assert calls["convert"] == [
        {"png": b"restyled-png-from-render as a woodcut", "is_generated": True}
    ]
    p = calls["publish"][0]
    # Restyled image is a generated frame — record the model + prompt.
    assert p["source"] == {
        "kind": "generated",
        "model": "gpt-image-2",
        "prompt": "render as a woodcut",
    }


def test_random_kind_uses_random_prompt(monkeypatch):
    calls = _install_fake_modules(monkeypatch)

    item = QueueItem(
        id="01HFTEST0003",
        enqueued_at="2026-05-13T14:00:00Z",
        source="cron",
        kind="random",
    )

    pipeline.process_item(item)

    assert calls["random_prompt"] == [True]
    chosen = "Geometric composition: overlapping circles"
    assert calls["generate"] == [chosen]
    # The item gets its prompt set in-place so manifest sees the subject.
    assert item.prompt == chosen
    p = calls["publish"][0]
    assert p["prompt"] == chosen
    assert p["source"] == {
        "kind": "generated",
        "model": "gpt-image-2",
        "prompt": chosen,
    }


def test_unknown_kind_raises(monkeypatch):
    _install_fake_modules(monkeypatch)
    item = QueueItem(
        id="01HFTEST0004",
        enqueued_at="2026-05-13T14:00:00Z",
        source="cli",
        kind="bogus",
    )
    with pytest.raises(ValueError):
        pipeline.process_item(item)


def test_image_kind_requires_key(monkeypatch):
    _install_fake_modules(monkeypatch)
    item = QueueItem(
        id="01HFTEST0005",
        enqueued_at="2026-05-13T14:00:00Z",
        source="cli",
        kind="image",
        image_s3_key=None,
    )
    with pytest.raises(ValueError):
        pipeline.process_item(item)


def test_openai_bad_request_becomes_permanent_item_error(monkeypatch):
    """OpenAI 400s (e.g. moderation_blocked) must surface as PermanentItemError.

    Without this translation, Lambda's async-invoke retry would redeliver
    the S3 event forever and pin the head of the queue. The generator
    handler is responsible for finalizing on PermanentItemError; the
    translation contract lives here.
    """
    from openai import BadRequestError

    _install_fake_modules(monkeypatch)

    # BadRequestError's __init__ expects a real httpx response — easier to
    # construct one via Exception.__new__ and seed the message attribute the
    # str() representation reads.
    moderation_error = BadRequestError.__new__(BadRequestError)
    Exception.__init__(
        moderation_error, "rejected by safety system: moderation_blocked"
    )

    def raises_moderation(prompt):
        raise moderation_error

    monkeypatch.setitem(
        sys.modules,
        "einkgen.core.generate",
        types.SimpleNamespace(
            generate=raises_moderation,
            generate_from_image=lambda *a, **k: b"",
            random_prompt=lambda: "unused",
            BASE_PROMPT="",
            PROMPT_LIBRARY=[],
            MODEL="gpt-image-2",
        ),
    )

    item = QueueItem(
        id="01HFTEST0007",
        enqueued_at="2026-05-13T14:00:00Z",
        source="admin",
        kind="prompt",
        prompt="something the safety system will reject",
    )

    with pytest.raises(pipeline.PermanentItemError):
        pipeline.process_item(item)


def test_non_bad_request_errors_propagate(monkeypatch):
    """Generic exceptions stay raw so Lambda retries them as transient."""
    _install_fake_modules(monkeypatch)

    def raises_runtime(prompt):
        raise RuntimeError("network blip")

    monkeypatch.setitem(
        sys.modules,
        "einkgen.core.generate",
        types.SimpleNamespace(
            generate=raises_runtime,
            generate_from_image=lambda *a, **k: b"",
            random_prompt=lambda: "unused",
            BASE_PROMPT="",
            PROMPT_LIBRARY=[],
            MODEL="gpt-image-2",
        ),
    )

    item = QueueItem(
        id="01HFTEST0008",
        enqueued_at="2026-05-13T14:00:00Z",
        source="admin",
        kind="prompt",
        prompt="transient failure case",
    )

    with pytest.raises(RuntimeError):
        pipeline.process_item(item)
