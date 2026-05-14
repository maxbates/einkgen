"""Publish primitive.

Writes `current/manifest.json` + `current/image.bmp`, archives the
frame under `history/<id>/`, and (when configured) invalidates the
matching CloudFront paths.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import boto3

from einkgen.core import s3
from einkgen.core.manifest import (
    DEFAULT_DISPLAY,
    Manifest,
    compute_next_check_after,
    compute_sha256,
    iso_utc,
)

CURRENT_MANIFEST_KEY = "current/manifest.json"
CURRENT_IMAGE_KEY = "current/image.bmp"


def _cdn_base() -> str:
    base = os.environ.get("EINKGEN_CDN_BASE")
    if not base:
        raise RuntimeError(
            "EINKGEN_CDN_BASE is not set; see .env.example for the expected value."
        )
    return base.rstrip("/")


def _read_previous_version() -> int:
    """Return the previous manifest's version, or 0 if none exists."""
    try:
        body = s3.get_object(CURRENT_MANIFEST_KEY)
    except Exception:
        # No previous manifest (NoSuchKey or any read error) — start at 1.
        return 0
    try:
        prev = Manifest.from_json(body)
    except Exception:
        return 0
    return int(prev.version)


def _invalidate_cloudfront(paths: list[str]) -> None:
    """Best-effort CloudFront invalidation; no-op if env var is unset."""
    distribution_id = os.environ.get("EINKGEN_CF_DISTRIBUTION_ID")
    if not distribution_id:
        return
    cf = boto3.client("cloudfront")
    cf.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": f"einkgen-{datetime.now(timezone.utc).timestamp()}",
        },
    )


def publish(
    processed_bmp: bytes,
    *,
    source: dict[str, Any],
    item_id: str,
    original: bytes | None = None,
    prompt: str | None = None,
    now: datetime | None = None,
) -> Manifest:
    """Publish a processed frame.

    Steps (see README §7, §8):
      1. Hash the BMP.
      2. Write `current/image.bmp`.
      3. Build and write `current/manifest.json` with an incremented version.
      4. Archive to `history/<item_id>/` (manifest, processed BMP, original).
      5. Invalidate CloudFront if `EINKGEN_CF_DISTRIBUTION_ID` is set.

    `prompt` is accepted for callers that want to override `source["prompt"]`;
    if passed, it is merged into the source dict written to the manifest.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    image_sha256 = compute_sha256(processed_bmp)
    image_bytes = len(processed_bmp)

    s3.put_object(CURRENT_IMAGE_KEY, processed_bmp, content_type="image/bmp")

    source_with_prompt = dict(source)
    if prompt is not None:
        source_with_prompt["prompt"] = prompt

    previous_version = _read_previous_version()
    next_check = compute_next_check_after(now)

    manifest = Manifest(
        version=previous_version + 1,
        generated_at=iso_utc(now),
        image_url=f"{_cdn_base()}/current/image.bmp",
        image_sha256=image_sha256,
        image_bytes=image_bytes,
        display=dict(DEFAULT_DISPLAY),
        next_check_after=iso_utc(next_check),
        source=source_with_prompt,
    )

    manifest_bytes = manifest.to_json().encode("utf-8")
    s3.put_object(
        CURRENT_MANIFEST_KEY,
        manifest_bytes,
        content_type="application/json",
    )

    # Archive. Each item id gets its own folder so re-delivery is idempotent.
    history_prefix = f"history/{item_id}"
    s3.put_object(
        f"{history_prefix}/manifest.json",
        manifest_bytes,
        content_type="application/json",
    )
    s3.put_object(
        f"{history_prefix}/processed.bmp",
        processed_bmp,
        content_type="image/bmp",
    )
    if original is not None:
        s3.put_object(
            f"{history_prefix}/original.png",
            original,
            content_type="image/png",
        )

    _invalidate_cloudfront([f"/{CURRENT_MANIFEST_KEY}", f"/{CURRENT_IMAGE_KEY}"])

    return manifest
