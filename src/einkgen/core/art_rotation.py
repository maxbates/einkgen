"""Subject and art-medium choices for automatic frames only."""

from __future__ import annotations

import random
from collections.abc import Sequence

# Plants and landscapes share one low-frequency lane; steampunk isn't a
# default. Use specific, usable briefs so the fallback is also varied.
LANES: dict[str, tuple[str, ...]] = {
    "animals": ("An octopus curling one giant arm", "A pangolin curled like a comma",
                "A shoebill in profile", "A manta ray viewed from below"),
    "people": ("A wrestler tying a single boot", "A diver seen through a round helmet",
               "A potter's hands shaping a bowl", "A drummer poised over one drum"),
    "objects": ("A split rubber stamp and its impression", "A folding fan half open",
                "A worn catcher's mitt", "A pair of shears opened like a bird"),
    "geometry": ("Two interlocking arcs with one deliberate break",
                 "An impossible folded-paper knot", "A black disk interrupted by a white wedge"),
    "motion": ("A figure skater in a deep lean", "A gymnast in mid-vault",
               "A porcelain cup tipping but not yet spilling"),
    "architecture": ("One spiral stair seen from above", "A rounded doorway casting a hard shadow",
                     "An arch of stacked stone isolated on white"),
    "science": ("A cutaway of a nautilus shell with bold divisions", "A moon in partial eclipse",
                "One ice crystal under a microscope, simplified to a few thick branches"),
    "surreal": ("A teacup whose handle casts the shadow of an elephant",
                "A fish opening like a book", "A chair casting a staircase-shaped shadow"),
    "nature": ("An isolated seed pod split down the middle",
               "A simplified coastal headland reduced to two masses", "A single wind-bent fern"),
}

MEDIA: tuple[str, ...] = (
    "Japanese-style paper cutout", "black-ink linocut", "torn-paper collage",
    "charcoal block drawing", "one-color screen print", "cut-paper photogram",
    "hard-light studio photography", "ink wash with broad shapes",
    "minimalist geometric poster without lettering", "clay relief lit from one side",
    "bold brush silhouette", "halftone comic with very coarse dots",
)

CUSTOM_LANE = "editors-pick"
RECENT_MEDIUM_WINDOW = 4


def _choose(options: Sequence[str], recent: Sequence[str], window: int) -> str:
    # With at least window+1 options, no category repeats in that window.
    # If the catalog shrinks, choose the least recently seen option.
    active = recent[:window]
    unused = [option for option in options if option not in active]
    return random.choice(unused) if unused else max(options, key=active.index)


def next_brief(
    *,
    recent_lanes: Sequence[str],
    recent_media: Sequence[str],
    custom_topics: Sequence[str],
) -> tuple[str, str, str]:
    """Return (lane, subject brief, medium), newest allocation first."""
    lanes = list(LANES)
    if custom_topics:
        lanes.append(CUSTOM_LANE)
    lane = _choose(lanes, recent_lanes, len(lanes) - 1)
    medium = _choose(MEDIA, recent_media, RECENT_MEDIUM_WINDOW)
    topic = random.choice(custom_topics if lane == CUSTOM_LANE else LANES[lane])
    return lane, topic, medium
