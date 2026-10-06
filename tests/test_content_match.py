from gp2sm.organize.content_match import assign, group_key


def test_group_key_strips_extension_case_and_counter():
    assert group_key("IMG_2155(1).HEIC") == "img_2155"
    assert group_key("IMG_2155.JPG") == "img_2155"
    assert group_key("lp_image(651).heic") == "lp_image"


def rot(h):
    return [h, 0xFFFF_FFFF_FFFF_FFFF, 0xFFFF_FFFF_FFFF_FFFF, 0xFFFF_FFFF_FFFF_FFFF]


def test_clean_match_with_margin():
    takeout = {1: ("img_1", rot(0b1010))}
    smug = {"A": ("img_1", 0b1011), "B": ("img_1", 0xFFFF_0000_FFFF_0000)}
    r = assign(takeout, smug)
    assert r[1]["decision"] == "match" and r[1]["image_key"] == "A" and r[1]["dist"] == 1


def test_no_margin_goes_to_review():
    takeout = {1: ("img_1", rot(0))}
    smug = {"A": ("img_1", 0b1), "B": ("img_1", 0b11)}  # two near-identical candidates
    r = assign(takeout, smug)
    assert r[1]["decision"] == "review"


def test_far_is_none():
    takeout = {1: ("img_1", rot(0))}
    smug = {"A": ("img_1", (1 << 30) - 1)}  # 30 bits differ
    assert assign(takeout, smug)[1]["decision"] == "none"


def test_each_smugmug_image_used_once_closest_wins():
    takeout = {1: ("g", rot(0b0)), 2: ("g", rot(0b11))}
    smug = {"A": ("g", 0b1)}  # item 1 at distance 1, item 2 at distance 1 too
    smug["A"] = ("g", 0b1)
    takeout[2] = ("g", rot(0b111))  # distance 2 -> loses
    r = assign(takeout, smug)
    assert r[1]["decision"] == "match" and r[2]["decision"] != "match"


def test_different_group_never_compared():
    takeout = {1: ("img_1", rot(0))}
    smug = {"A": ("img_2", 0)}
    assert assign(takeout, smug)[1]["decision"] == "none"


def test_assign_bursts_one_to_one_closest_first():
    from gp2sm.organize.content_match import assign_bursts
    takeout = {1: rot(0b0), 2: rot(0b1), 3: rot((1 << 40) - 1)}
    smug = {"A": 0b0, "B": 0b11}
    paired, unpaired = assign_bursts(takeout, smug)
    assert paired[1] == ("A", 0)          # exact frame wins A
    assert paired[2] == ("B", 1)          # next closest takes B
    assert unpaired == [3]                # nothing left near it -> upload


def test_assign_bursts_respects_max_dist():
    from gp2sm.organize.content_match import assign_bursts
    paired, unpaired = assign_bursts({1: rot((1 << 10) - 1)}, {"A": 0}, max_dist=6)
    assert paired == {} and unpaired == [1]
