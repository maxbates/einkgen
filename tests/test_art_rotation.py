from collections import Counter

from einkgen.core import art_rotation


def test_subject_and_media_rotation():
    lanes, media = [], []
    for _ in range(90):
        lane, topic, medium = art_rotation.next_brief(
            recent_lanes=lanes, recent_media=media, custom_topics=(),
        )
        assert lane not in lanes[:8]
        assert medium not in media[:4]
        assert topic in art_rotation.LANES[lane]
        lanes.insert(0, lane)
        media.insert(0, medium)

    counts = Counter(lanes)
    assert counts == {lane: 10 for lane in art_rotation.LANES}
    assert "steampunk" not in " ".join(t for ts in art_rotation.LANES.values() for t in ts).lower()


def test_custom_library_is_one_lane_not_the_whole_mix():
    lanes = []
    custom = ("A steampunk clock",)
    for _ in range(60):
        lane, topic, _ = art_rotation.next_brief(
            recent_lanes=lanes, recent_media=(), custom_topics=custom,
        )
        assert (topic in custom) == (lane == art_rotation.CUSTOM_LANE)
        lanes.insert(0, lane)
    assert lanes.count(art_rotation.CUSTOM_LANE) == 6
    assert lanes.count("nature") == 6
