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
        "random_prompt": [],
        "convert": [],
        "publish": [],
    }

    def fake_generate(prompt):
        calls["generate"].append(prompt)
        return b"fake-png-bytes-for-" + (prompt or "").encode()

    def fake_random_prompt():
        calls["random_prompt"].append(True)
        return "Geometric composition: overlapping circles"

    def fake_convert(png):
        calls["convert"].append(png)
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
        random_prompt=fake_random_prompt,
        BASE_PROMPT="",
        PROMPT_LIBRARY=[],
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
    assert calls["convert"][0].startswith(b"fake-png-bytes-for-")
    assert len(calls["publish"]) == 1
    p = calls["publish"][0]
    assert p["item_id"] == "01HFTEST0001"
    assert p["prompt"] == "a foggy cliff at dawn"
    assert p["source"] == {
        "kind": "generated",
        "model": "gpt-image-1",
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
    # Convert sees the bytes we fetched from S3.
    assert calls["convert"] == [b"real-jpeg-bytes"]
    p = calls["publish"][0]
    assert p["item_id"] == "01HFTEST0002"
    assert p["original"] == b"real-jpeg-bytes"
    # README §7: model/prompt are omitted for image-kind uploads,
    # not present-with-null.
    assert p["source"] == {"kind": "uploaded"}
    assert "model" not in p["source"]
    assert "prompt" not in p["source"]
    assert p["prompt"] is None

    # Staged source must have been cleaned up after successful publish.
    assert "Contents" not in s3_bucket.list_objects_v2(
        Bucket=TEST_BUCKET, Prefix=staged_key
    )


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
        "model": "gpt-image-1",
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
