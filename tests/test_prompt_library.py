"""Tests for the S3-backed random-pick prompt library."""

from __future__ import annotations

import pytest

from einkgen.core import prompt_library as pl
from tests.conftest import TEST_BUCKET


@pytest.fixture
def reset_cache():
    pl._reset_cache()
    yield
    pl._reset_cache()


def test_missing_file_falls_back_to_defaults(s3_bucket, reset_cache):
    """First-deploy state: no file → DEFAULTS, never empty."""
    current = pl.load()
    assert current == pl.DEFAULTS
    # And it's a tuple — immutable contract for callers.
    assert isinstance(current, tuple)


def test_empty_file_falls_back_to_defaults(s3_bucket, reset_cache):
    """An accidentally-empty file shouldn't crash random.choice."""
    s3_bucket.put_object(Bucket=TEST_BUCKET, Key=pl.PROMPT_LIBRARY_KEY, Body=b"")
    assert pl.load() == pl.DEFAULTS


def test_parse_preserves_order_and_dedupes(s3_bucket, reset_cache):
    body = (
        "# header\n"
        "\n"
        "Apple — bold red fruit.\n"
        "Banana — yellow curve.\n"
        "Apple — bold red fruit.\n"  # duplicate, dropped
        "Cherry — small round.  # trailing comment\n"
        "  # indented comment\n"
    )
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=pl.PROMPT_LIBRARY_KEY, Body=body.encode()
    )
    assert pl.load() == (
        "Apple — bold red fruit.",
        "Banana — yellow curve.",
        "Cherry — small round.",
    )


def test_write_round_trip_and_cache_invalidation(s3_bucket, reset_cache):
    persisted = pl.write(
        ["  one  ", "two", "one", "", "# skipped", "three"]
    )
    assert persisted == ("one", "two", "three")
    # Cache invalidated → load() reflects the new state.
    assert pl.load() == ("one", "two", "three")
    body = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=pl.PROMPT_LIBRARY_KEY)[
        "Body"
    ].read().decode()
    assert body.startswith("# einkgen topic library")
    # Entries are persisted in input order — operator can group related prompts.
    payload_lines = [l for l in body.splitlines() if l and not l.startswith("#")]
    assert payload_lines == ["one", "two", "three"]


def test_write_rejects_empty_library(s3_bucket, reset_cache):
    with pytest.raises(ValueError, match="at least one entry"):
        pl.write([])
    with pytest.raises(ValueError, match="at least one entry"):
        pl.write(["   ", "# only comments"])


def test_reset_to_defaults_writes_seed(s3_bucket, reset_cache):
    pl.write(["custom one", "custom two"])
    assert pl.load() == ("custom one", "custom two")
    restored = pl.reset_to_defaults()
    assert restored == pl.DEFAULTS
    assert pl.load() == pl.DEFAULTS


def test_random_prompt_picks_from_current_library(s3_bucket, reset_cache):
    pl.write(["only-option"])
    assert pl.random_prompt() == "only-option"


def test_cache_amortises_repeat_loads(s3_bucket, reset_cache, monkeypatch):
    """Warm-Lambda invocations shouldn't re-fetch the file on every call."""
    pl.write(["a", "b"])
    head_calls = {"n": 0}
    real_head = pl.s3.head_object

    def counting_head(key):
        head_calls["n"] += 1
        return real_head(key)

    monkeypatch.setattr(pl.s3, "head_object", counting_head)
    pl.load()  # first call → populates cache (1 head)
    pl.load()  # cached
    pl.load()  # cached
    assert head_calls["n"] == 1


def test_force_load_bypasses_cache(s3_bucket, reset_cache):
    """force=True is the escape hatch for the CLI / admin GET."""
    pl.write(["first"])
    assert pl.load() == ("first",)
    # Edit the object out-of-band — cache still holds the old value.
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=pl.PROMPT_LIBRARY_KEY, Body=b"second\n"
    )
    assert pl.load() == ("first",)
    assert pl.load(force=True) == ("second",)
