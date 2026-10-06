from gp2sm.date_undated import pick_by_content, pick_by_video_shape, single_month, stem_key
from gp2sm.media import mp4_duration


def rot(h):
    return [h, (1 << 64) - 1, (1 << 64) - 1, (1 << 64) - 1]


def test_stem_key():
    assert stem_key("IMG_0416.MOV") == stem_key("img_0416(2).mov") == "img_0416"


def test_single_month():
    assert single_month([1696118400, 1697000000], "UTC") == "2023-10"
    assert single_month([1696118400, 1704067200], "UTC") is None


def test_video_shape_unique_match():
    cands = [("a", 12.5, 1.78, 100), ("b", 30.0, 1.78, 100)]
    assert pick_by_video_shape((12.49, 1.78, 100), cands) == "a"
    assert pick_by_video_shape((12.49, 1.33, 100), cands) is None                                   # aspect differs
    assert pick_by_video_shape((12.5, 1.78, 100), [("a", 12.5, 1.78, 100), ("b", 12.6, 1.78, 100)]) is None  # not unique
    assert pick_by_video_shape((None, 1.78, 100), cands) is None


def test_content_pick_with_margin():
    assert pick_by_content(0, {"a": rot(0b1), "b": rot((1 << 30) - 1)})[0] == "a"
    assert pick_by_content(0, {"a": rot(0b1), "b": rot(0b11)})[0] is None          # no margin
    assert pick_by_content(0, {"a": rot((1 << 20) - 1)})[0] is None                # too far


def test_mp4_duration_v0():
    box = b"mvhd" + bytes([0, 0, 0, 0]) + b"\0" * 8 + (600).to_bytes(4, "big") + (7494).to_bytes(4, "big")
    assert abs(mp4_duration(b"...." + box) - 12.49) < 0.001


def test_stem_key_strips_download_prefix():
    assert stem_key("dl_1745987855056_IMG_1290.jpg") == "img_1290"


def test_video_shape_rules_no_upscale_and_relative_tolerance():
    from gp2sm.date_undated import video_shape_hits
    # destination 360x638 (229,680 px); 108x192 candidate is far smaller -> can't be the source
    assert video_shape_hits((37.23, 1.772, 229680), [("big", 37.23, 1.769, 229320), ("tiny", 37.23, 1.778, 20736)]) == ["big"]
    # re-encode padded 11.96 s to 12.7 s: within 7%
    assert video_shape_hits((12.7, 1.778, 2073600), [("a", 11.96, 1.778, 2073600), ("b", 0.75, 1.778, 2073600)]) == ["a"]


def test_near_matches():
    from gp2sm.date_undated import near_matches
    assert sorted(near_matches(0, {"a": rot(0), "b": rot(0b1), "c": rot((1 << 30) - 1)})) == ["a", "b"]
