"""Publish + archive primitives.

Two distinct write paths:

- ``archive_to_history`` writes a rendered frame to ``history/<id>/``
  WITHOUT touching ``current/``. The cron-driven render path calls
  this and then enqueues a marker into the generated queue
  (``core.generated_queue``); the device only sees the frame after a
  ``/wake`` advance pops the marker.

- ``set_current_from_history`` and ``publish`` write ``current/manifest.json``
  (the device's read target). ``set_current_from_history`` re-points
  the manifest at an existing archive without copying bytes — used by
  ``/wake`` and the admin **Show this now** button. ``publish`` is the
  legacy combined "archive + set current in one shot" entrypoint, kept
  for admin-driven "render now"/"render item" overrides (where the
  operator wants the image on the panel immediately rather than via the
  generated queue).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

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


class HistoryItemNotFound(Exception):
    """Raised when ``set_current_from_history`` can't find ``history/<id>/manifest.json``."""

    def __init__(self, history_id: str):
        super().__init__(f"history item not found: {history_id!r}")
        self.history_id = history_id


def _cdn_base() -> str:
    base = os.environ.get("EINKGEN_CDN_BASE")
    if not base:
        raise RuntimeError(
            "EINKGEN_CDN_BASE is not set; see .env.example for the expected value."
        )
    return base.rstrip("/")


_MISSING_OBJECT_CODES = {"NoSuchKey", "NotFound", "404"}


def _poll_interval() -> timedelta:
    """Read ``EINKGEN_POLL_INTERVAL_SECONDS`` and return a timedelta.

    Falls back to ``compute_next_check_after``'s own default (1 hour) when
    the env var is unset, empty, or unparseable. We intentionally silently
    fall back on bad values: a malformed override shouldn't take the
    publish path down.
    """
    raw = os.environ.get("EINKGEN_POLL_INTERVAL_SECONDS", "").strip()
    if not raw:
        return timedelta(hours=1)
    try:
        seconds = int(raw)
    except ValueError:
        return timedelta(hours=1)
    if seconds <= 0:
        return timedelta(hours=1)
    return timedelta(seconds=seconds)


def _read_previous_manifest_meta() -> tuple[int, str | None]:
    """Return ``(previous_version, etag)`` for the current manifest.

    Returns ``(0, None)`` if no manifest exists yet (first publish). A
    malformed body is treated as fresh — same as ``_read_previous_version``
    used to — so the publish path can recover from a corrupt manifest
    without operator intervention. Any other S3 error is re-raised; we
    don't want to silently restart versions at 1 and shadow history.
    """
    try:
        body, etag = s3.get_object_with_etag(CURRENT_MANIFEST_KEY)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in _MISSING_OBJECT_CODES:
            return 0, None
        raise
    try:
        prev = Manifest.from_json(body)
    except (ValueError, KeyError):
        return 0, etag
    return int(prev.version), etag


# How many times to retry a current-manifest write before giving up. Two
# wakes racing within the same RTT to S3 is the realistic worst case; a
# handful of retries covers that without unbounded spin.
_MANIFEST_CAS_MAX_ATTEMPTS = 6


def _write_current_manifest_cas(
    build_manifest,
) -> Manifest:
    """Compare-and-swap loop around ``current/manifest.json``.

    ``build_manifest(previous_version: int) -> Manifest`` is called once
    per attempt with the freshly-read previous version so the caller can
    set ``version = previous + 1``. On collision (another writer beat us
    between our read and our put) we re-read and rebuild rather than
    blindly retrying with stale data — that's what keeps the version
    field monotonic under concurrent ``/wake`` calls (TODOS race fix).
    """
    last_exc: ClientError | None = None
    for _ in range(_MANIFEST_CAS_MAX_ATTEMPTS):
        previous_version, etag = _read_previous_manifest_meta()
        manifest = build_manifest(previous_version)
        manifest_bytes = manifest.to_json().encode("utf-8")
        try:
            if etag is None:
                s3.put_object(
                    CURRENT_MANIFEST_KEY,
                    manifest_bytes,
                    content_type="application/json",
                    if_none_match="*",
                )
            else:
                s3.put_object(
                    CURRENT_MANIFEST_KEY,
                    manifest_bytes,
                    content_type="application/json",
                    if_match=etag,
                )
            return manifest
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                last_exc = exc
                continue
            raise
    raise RuntimeError(
        f"compare-and-swap on {CURRENT_MANIFEST_KEY} failed after "
        f"{_MANIFEST_CAS_MAX_ATTEMPTS} attempts; last error: {last_exc!r}"
    )


def _invalidate_cloudfront(paths: list[str]) -> None:
    """Best-effort CloudFront invalidation; no-op if env var is unset.

    ``CallerReference`` must be unique per invalidation per distribution
    (CloudFront rejects duplicates with ``InvalidationBatchAlreadyExists``).
    Two near-simultaneous calls — e.g. cron landing the same tick as a
    ``/wake`` advance — would collide on a timestamp-based reference, so
    we use a UUID. Any failure is logged and swallowed; the manifest
    write already succeeded, and the next invalidation (or CloudFront's
    natural TTL expiry) closes the cache-staleness window.
    """
    distribution_id = os.environ.get("EINKGEN_CF_DISTRIBUTION_ID")
    if not distribution_id:
        return
    try:
        boto3.client("cloudfront").create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": len(paths), "Items": paths},
                "CallerReference": f"einkgen-{uuid.uuid4()}",
            },
        )
    except Exception:
        # Catch `Exception`, not just ``ClientError``: boto3 also raises
        # ``EndpointConnectionError`` / ``ReadTimeoutError`` /
        # ``NoCredentialsError`` (botocore.exceptions) which are not
        # ``ClientError`` subclasses. Letting any of those propagate after
        # the manifest has already been written leaves the Lambda raising
        # while ``current/`` has advanced — the device would fetch the
        # stale manifest from CDN until natural TTL expiry. Swallow + log
        # so the manifest write stays load-bearing.
        log.exception("cloudfront invalidation failed for %s", paths)


def archive_to_history(
    processed_bmp: bytes,
    *,
    source: dict[str, Any],
    item_id: str,
    original: bytes | None = None,
    prompt: str | None = None,
    now: datetime | None = None,
) -> Manifest:
    """Write a rendered frame under ``history/<item_id>/``.

    Does NOT touch ``current/`` — that's left for ``/wake`` to update via
    ``set_current_from_history`` once the device is ready to draw this
    frame. The cron render path lands here.

    Returns the manifest written under ``history/<item_id>/manifest.json``
    so callers can stash the metadata (sha, bytes, source) on a marker
    in the generated queue without re-hashing the bmp.

    The history manifest's ``image_url`` already points at the
    ``history/<id>/processed.bmp`` archive — that's the URL
    ``set_current_from_history`` re-uses when promoting to current.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    image_sha256 = compute_sha256(processed_bmp)
    image_bytes = len(processed_bmp)

    source_with_prompt = dict(source)
    if prompt is not None:
        source_with_prompt["prompt"] = prompt

    # next_check_after on a history manifest is mostly advisory — the
    # device never reads it directly; ``set_current_from_history``
    # recomputes a fresh one when promoting to current. We still write
    # one for completeness so the history manifest validates against the
    # same dataclass.
    next_check = compute_next_check_after(now, tick_interval=_poll_interval())

    manifest = Manifest(
        # version is per-frame here, not the global current-manifest version
        # (which is only meaningful for ``current/manifest.json``). We carry
        # 1 so the dataclass is happy.
        version=1,
        generated_at=iso_utc(now),
        image_url=f"{_cdn_base()}/history/{item_id}/processed.bmp",
        image_sha256=image_sha256,
        image_bytes=image_bytes,
        display=dict(DEFAULT_DISPLAY),
        next_check_after=iso_utc(next_check),
        source=source_with_prompt,
    )

    manifest_bytes = manifest.to_json().encode("utf-8")
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
    return manifest


def publish(
    processed_bmp: bytes,
    *,
    source: dict[str, Any],
    item_id: str,
    original: bytes | None = None,
    prompt: str | None = None,
    now: datetime | None = None,
) -> Manifest:
    """Archive a frame AND set it as the current frame in one shot.

    Legacy entrypoint kept for the admin "render now" / "render item"
    overrides — both call this so the operator sees the new image on
    the panel without going through the generated queue. Cron-driven
    renders use ``archive_to_history`` + the generated-queue marker
    flow instead.

    Steps (see ARCHITECTURE §7, §8):
      1. Hash the BMP.
      2. Write `current/image.bmp`.
      3. Build and write `current/manifest.json` with an incremented version.
      4. Archive to `history/<item_id>/` (manifest, processed BMP, original).
      5. Invalidate CloudFront if `EINKGEN_CF_DISTRIBUTION_ID` is set.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    image_sha256 = compute_sha256(processed_bmp)
    image_bytes = len(processed_bmp)

    s3.put_object(CURRENT_IMAGE_KEY, processed_bmp, content_type="image/bmp")

    source_with_prompt = dict(source)
    if prompt is not None:
        source_with_prompt["prompt"] = prompt

    next_check = compute_next_check_after(now, tick_interval=_poll_interval())

    def _build(previous_version: int) -> Manifest:
        return Manifest(
            version=previous_version + 1,
            generated_at=iso_utc(now),
            image_url=f"{_cdn_base()}/current/image.bmp",
            image_sha256=image_sha256,
            image_bytes=image_bytes,
            display=dict(DEFAULT_DISPLAY),
            next_check_after=iso_utc(next_check),
            source=source_with_prompt,
        )

    manifest = _write_current_manifest_cas(_build)
    manifest_bytes = manifest.to_json().encode("utf-8")

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


def set_current_from_history(
    history_id: str,
    *,
    now: datetime | None = None,
) -> Manifest:
    """Re-publish an existing history frame as the current one.

    The history bytes are not copied or regenerated — we just write a new
    ``current/manifest.json`` whose ``image_url`` points at the existing
    ``history/<id>/processed.bmp`` and whose ``image_sha256`` / ``image_bytes``
    are carried over verbatim. The device, on its next poll, sees a new
    manifest version + a new (to-it) sha256 and downloads from the history
    URL. The next normal generation overwrites the manifest back to
    ``current/image.bmp``.

    ``source.replayed_from`` is set to ``history_id`` so the SPA can mark the
    tile as currently-showing even if two history items happen to share a
    sha256.

    Raises ``HistoryItemNotFound`` if no manifest exists at
    ``history/<history_id>/manifest.json``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    history_manifest_key = f"history/{history_id}/manifest.json"
    try:
        body = s3.get_object(history_manifest_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in _MISSING_OBJECT_CODES:
            raise HistoryItemNotFound(history_id) from exc
        raise
    history_manifest = Manifest.from_json(body)

    next_check = compute_next_check_after(now, tick_interval=_poll_interval())

    source = dict(history_manifest.source)
    source["replayed_from"] = history_id

    def _build(previous_version: int) -> Manifest:
        return Manifest(
            version=previous_version + 1,
            generated_at=iso_utc(now),
            image_url=f"{_cdn_base()}/history/{history_id}/processed.bmp",
            image_sha256=history_manifest.image_sha256,
            image_bytes=history_manifest.image_bytes,
            display=dict(DEFAULT_DISPLAY),
            next_check_after=iso_utc(next_check),
            source=source,
        )

    manifest = _write_current_manifest_cas(_build)
    # image_url changed; only the manifest needs CF invalidation. The
    # `history/<id>/processed.bmp` it now points at is already CDN-cached.
    _invalidate_cloudfront([f"/{CURRENT_MANIFEST_KEY}"])
    return manifest
