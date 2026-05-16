"""Tests for the S3-backed email allowlist."""

from __future__ import annotations

import pytest

from einkgen.core import email_allowlist as al
from tests.conftest import TEST_BUCKET


@pytest.fixture
def reset_cache():
    al._reset_cache()
    yield
    al._reset_cache()


def test_missing_file_means_empty_allowlist(s3_bucket, reset_cache):
    """First-deploy state: no file → empty set → all senders rejected."""
    assert al.load() == frozenset()
    assert al.is_allowed("anyone@example.com") is False


def test_parse_skips_blanks_and_comments(s3_bucket, reset_cache):
    body = (
        "# header comment\n"
        "\n"
        "Me@Example.com\n"
        "spouse@example.com  # trailing comment\n"
        "  # indented comment\n"
        "\n"
    )
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=al.ALLOWLIST_KEY, Body=body.encode()
    )
    assert al.load() == frozenset({"me@example.com", "spouse@example.com"})


def test_is_allowed_is_case_insensitive(s3_bucket, reset_cache):
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=al.ALLOWLIST_KEY, Body=b"me@example.com\n"
    )
    assert al.is_allowed("ME@Example.COM") is True
    assert al.is_allowed("me@example.com") is True
    assert al.is_allowed("") is False
    assert al.is_allowed(None) is False
    assert al.is_allowed("other@example.com") is False


def test_write_round_trip(s3_bucket, reset_cache):
    al.write(["b@example.com", "A@Example.com", "b@example.com", "  "])
    body = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=al.ALLOWLIST_KEY)[
        "Body"
    ].read().decode()
    # Header comment present; entries deduped, lowercased, sorted.
    assert body.startswith("# einkgen email allowlist")
    lines = [l for l in body.splitlines() if l and not l.startswith("#")]
    assert lines == ["a@example.com", "b@example.com"]
    # Cache is invalidated after write — next is_allowed reflects the new state.
    assert al.is_allowed("a@example.com") is True
