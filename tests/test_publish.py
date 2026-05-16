"""Tests for the publish primitive."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import boto3
import pytest

from einkgen.core import publish
from einkgen.core.manifest import Manifest, compute_sha256


def _cf_only_patch(fake_cf):
    """Patch publish.boto3.client so ONLY ``cloudfront`` is faked.

    The earlier broad ``return_value=fake_cf`` form replaced every boto3.client
    call (including the s3 reads we need), which masked real bugs in
    publish's previous-manifest read path.
    """
    real_client = boto3.client

    def select(service, *args, **kwargs):
        if service == "cloudfront":
            return fake_cf
        return real_client(service, *args, **kwargs)

    return patch("einkgen.core.publish.boto3.client", side_effect=select)


PROCESSED = b"BMP" + b"\x00" * 100  # stand-in BMP payload
ORIGINAL = b"\x89PNG" + b"\x00" * 50


def test_publish_writes_current_and_history(s3_bucket):
    item_id = "01HF7ZTEST"
    source = {"kind": "generated", "model": "gpt-image-1", "prompt": "a cliff"}

    manifest = publish.publish(
        PROCESSED,
        source=source,
        item_id=item_id,
        original=ORIGINAL,
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )

    assert manifest.version == 1
    assert manifest.image_sha256 == compute_sha256(PROCESSED)
    assert manifest.image_bytes == len(PROCESSED)
    assert manifest.image_url.endswith("/current/image.bmp")
    assert manifest.display == {"width": 1200, "height": 825, "levels": 8}
    # 14:00 falls exactly on a tick → next check is the *next* 1 h tick
    # (15:00) + 5m buffer. See compute_next_check_after for the epsilon
    # behaviour at exact boundaries.
    assert manifest.next_check_after == "2026-05-13T15:05:00Z"
    assert manifest.generated_at == "2026-05-13T14:00:00Z"

    # current/image.bmp + current/manifest.json
    image = s3_bucket.get_object(Bucket="einkgen-test", Key="current/image.bmp")
    assert image["Body"].read() == PROCESSED
    assert image["ContentType"] == "image/bmp"

    manifest_obj = s3_bucket.get_object(
        Bucket="einkgen-test", Key="current/manifest.json"
    )
    assert manifest_obj["ContentType"] == "application/json"
    on_disk = Manifest.from_json(manifest_obj["Body"].read())
    assert on_disk == manifest

    # history/<id>/{manifest.json, processed.bmp, original.png}
    h_manifest = s3_bucket.get_object(
        Bucket="einkgen-test", Key=f"history/{item_id}/manifest.json"
    )
    assert Manifest.from_json(h_manifest["Body"].read()) == manifest

    h_processed = s3_bucket.get_object(
        Bucket="einkgen-test", Key=f"history/{item_id}/processed.bmp"
    )
    assert h_processed["Body"].read() == PROCESSED
    assert h_processed["ContentType"] == "image/bmp"

    h_original = s3_bucket.get_object(
        Bucket="einkgen-test", Key=f"history/{item_id}/original.png"
    )
    assert h_original["Body"].read() == ORIGINAL
    assert h_original["ContentType"] == "image/png"


def test_publish_omits_original_when_not_provided(s3_bucket):
    publish.publish(
        PROCESSED,
        source={"kind": "upload"},
        item_id="01HF7ZNOIMG",
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )

    # original.png should not exist
    listed = s3_bucket.list_objects_v2(
        Bucket="einkgen-test", Prefix="history/01HF7ZNOIMG/"
    )
    keys = {o["Key"] for o in listed.get("Contents", [])}
    assert "history/01HF7ZNOIMG/manifest.json" in keys
    assert "history/01HF7ZNOIMG/processed.bmp" in keys
    assert "history/01HF7ZNOIMG/original.png" not in keys


def test_publish_increments_version_off_previous_manifest(s3_bucket):
    base_now = datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc)

    first = publish.publish(
        PROCESSED, source={"kind": "generated"}, item_id="id-1", now=base_now
    )
    second = publish.publish(
        PROCESSED + b"X", source={"kind": "generated"}, item_id="id-2", now=base_now
    )
    third = publish.publish(
        PROCESSED + b"XY", source={"kind": "generated"}, item_id="id-3", now=base_now
    )

    assert (first.version, second.version, third.version) == (1, 2, 3)


def test_publish_skips_cf_invalidation_when_env_var_absent(s3_bucket, monkeypatch):
    monkeypatch.delenv("EINKGEN_CF_DISTRIBUTION_ID", raising=False)

    fake_cf = MagicMock()
    with _cf_only_patch(fake_cf) as p:
        publish.publish(
            PROCESSED,
            source={"kind": "generated"},
            item_id="id-cf-off",
            now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
        )

    # boto3.client should never be called for CloudFront.
    assert not any(
        call.args and call.args[0] == "cloudfront" for call in p.call_args_list
    )
    fake_cf.create_invalidation.assert_not_called()


def test_publish_invalidates_cf_when_env_var_set(s3_bucket, monkeypatch):
    monkeypatch.setenv("EINKGEN_CF_DISTRIBUTION_ID", "E123ABC")

    fake_cf = MagicMock()
    with _cf_only_patch(fake_cf):
        publish.publish(
            PROCESSED,
            source={"kind": "generated"},
            item_id="id-cf-on",
            now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
        )

    fake_cf.create_invalidation.assert_called_once()
    kwargs = fake_cf.create_invalidation.call_args.kwargs
    assert kwargs["DistributionId"] == "E123ABC"
    paths = kwargs["InvalidationBatch"]["Paths"]["Items"]
    assert set(paths) == {"/current/manifest.json", "/current/image.bmp"}


def test_publish_honours_poll_interval_env_var(s3_bucket, monkeypatch):
    # 900 seconds = 15 minutes. 14:00 UTC → next 15-min tick after the
    # epsilon is 14:15, plus the default 5m buffer = 14:20.
    monkeypatch.setenv("EINKGEN_POLL_INTERVAL_SECONDS", "900")
    m = publish.publish(
        PROCESSED,
        source={"kind": "generated"},
        item_id="id-poll",
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )
    assert m.next_check_after == "2026-05-13T14:20:00Z"


def test_publish_falls_back_when_poll_interval_env_unparseable(s3_bucket, monkeypatch):
    # A malformed override silently falls back to the 1 h default rather
    # than taking the publish path down.
    monkeypatch.setenv("EINKGEN_POLL_INTERVAL_SECONDS", "not-a-number")
    m = publish.publish(
        PROCESSED,
        source={"kind": "generated"},
        item_id="id-poll-bad",
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )
    assert m.next_check_after == "2026-05-13T15:05:00Z"


@pytest.mark.parametrize("bad_value", ["0", "-5"])
def test_publish_falls_back_when_poll_interval_env_nonpositive(
    s3_bucket, monkeypatch, bad_value
):
    # The ``seconds <= 0`` guard in _poll_interval must also fall back to
    # the 1 h default — a zero or negative interval would otherwise round
    # the next-check to "right now" and DoS the device.
    monkeypatch.setenv("EINKGEN_POLL_INTERVAL_SECONDS", bad_value)
    m = publish.publish(
        PROCESSED,
        source={"kind": "generated"},
        item_id=f"id-poll-{bad_value.replace('-', 'neg')}",
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )
    assert m.next_check_after == "2026-05-13T15:05:00Z"


def test_publish_prompt_kwarg_overrides_source(s3_bucket):
    m = publish.publish(
        PROCESSED,
        source={"kind": "generated", "model": "gpt-image-1"},
        item_id="id-prompt",
        prompt="overridden prompt",
        now=datetime(2026, 5, 13, 14, 0, 0, tzinfo=timezone.utc),
    )
    assert m.source["prompt"] == "overridden prompt"
    assert m.source["kind"] == "generated"
