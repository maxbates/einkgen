"""Tests for the angle-token bag used by ``expand_topic`` steering."""

from __future__ import annotations

import random

from einkgen.core import angles


def test_axes_are_non_empty_and_unique_within_axis():
    """Each axis ships with a usable bag of phrases, no duplicates."""
    assert angles.AXES, "AXES must define at least one axis"
    for name, bag in angles.AXES.items():
        assert bag, f"axis {name!r} must be non-empty"
        assert len(bag) == len(set(bag)), f"axis {name!r} has duplicates"


def test_sample_angles_returns_one_phrase_per_chosen_axis():
    """Default n_axes=2 picks one phrase from each of two distinct axes."""
    rng = random.Random(0)
    picks = angles.sample_angles(rng=rng)
    assert len(picks) == 2
    # All sampled phrases come from some axis bag.
    flat = {p for bag in angles.AXES.values() for p in bag}
    for phrase in picks:
        assert phrase in flat


def test_sample_angles_zero_returns_empty():
    assert angles.sample_angles(n_axes=0) == []


def test_sample_angles_caps_at_axis_count():
    """Requesting more axes than exist yields one pick per axis, no error."""
    rng = random.Random(1)
    picks = angles.sample_angles(n_axes=99, rng=rng)
    assert len(picks) == len(angles.AXES)


def test_sample_angles_uses_provided_rng_deterministically():
    a = angles.sample_angles(rng=random.Random(42))
    b = angles.sample_angles(rng=random.Random(42))
    assert a == b


def test_sample_angles_diversity_across_many_calls():
    """Across 200 calls we should see a wide spread of phrases.

    Lower bound is loose — we just want to catch a regression where the
    sampler accidentally locks onto a single axis or phrase.
    """
    rng = random.Random(7)
    seen: set[str] = set()
    for _ in range(200):
        seen.update(angles.sample_angles(rng=rng))
    # 50 distinct phrases out of ~200 is easy with two random axes of
    # 25–60 entries each. If this fails the sampler is degenerate.
    assert len(seen) >= 50
