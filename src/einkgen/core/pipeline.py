"""One queue item -> a published frame.

Lazy-imports ``generate``, ``convert``, ``publish`` so this module loads cleanly
in worktrees where those siblings have not been written yet (and so tests can
inject mocks via ``sys.modules``).
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import boto3

from einkgen.core.queue import QueueItem


def _fetch_s3_bytes(key: str) -> bytes:
    bucket = os.environ.get("EINKGEN_BUCKET")
    if not bucket:
        raise RuntimeError("EINKGEN_BUCKET env var is not set")
    obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def process_item(item: QueueItem) -> None:
    """Generate (or fetch) -> convert -> publish."""
    generate = importlib.import_module("einkgen.core.generate")
    convert_mod = importlib.import_module("einkgen.core.convert")
    publish_mod = importlib.import_module("einkgen.core.publish")

    original_png: bytes
    if item.kind == "prompt":
        # BASE_PROMPT is prepended inside generate.generate.
        original_png = generate.generate(item.prompt)
    elif item.kind == "image":
        if not item.image_s3_key:
            raise ValueError(f"image item {item.id} has no image_s3_key")
        original_png = _fetch_s3_bytes(item.image_s3_key)
    elif item.kind == "random":
        prompt = generate.random_prompt()
        item.prompt = prompt  # so publish/manifest can record the chosen subject
        original_png = generate.generate(prompt)
    else:
        raise ValueError(f"unknown kind: {item.kind!r}")

    processed_bmp = convert_mod.convert(original_png)

    source: dict[str, Any] = {
        "kind": "generated" if item.kind != "image" else "uploaded",
        "model": "gpt-image-1" if item.kind != "image" else None,
        "prompt": item.prompt,
    }

    publish_mod.publish(
        processed_bmp,
        source=source,
        item_id=item.id,
        original=original_png,
        prompt=item.prompt,
    )
