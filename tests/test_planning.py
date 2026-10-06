from gp2sm.organize import planning

CFG = {
    "photo_album_template": "P {yyyy}-{mm}",
    "video_album_template": "V {yyyy}",
    "undated_photo_album": "P Undated",
    "undated_video_album": "V Undated",
    "duplicates_album": "Dupes",
    "album_soft_cap": 2,
}


def legacy(gid, filename, ts, w=100, h=50):
    return {"google_id": gid, "filename": filename, "creation_ts": ts, "width": w, "height": h}


def img(key, filename, md5=None, is_video=0, w=100, h=50, uploaded="2025-01-01T00:00:00"):
    return {"item_id": key, "filename": filename, "md5": md5, "is_video": is_video,
            "width": w, "height": h, "uploaded": uploaded}


def index(items, md5s=()):
    return planning.LegacyIndex(items, [{"google_id": g, "md5": m} for g, m in md5s])


def test_parse_duration():
    assert planning.parse_duration("6.08 s") == 6.08
    assert planning.parse_duration("4") == 4.0
    assert planning.parse_duration("") is None


def test_to_local_crosses_month_boundary():
    # 03:00 UTC on Jun 1 is still May 31 in New York
    assert planning.to_local("2023-06-01T03:00:00Z", "America/New_York") == "2023-05-31T23:00:00"


def test_md5_match_is_high_confidence():
    idx = index([legacy("g1", "IMG_1.JPG", "2023-05-01T12:00:00Z")], [("g1", "abc")])
    m = planning.match_image(img("k1", "whatever.jpg", md5="abc"), idx, "UTC")
    assert (m["google_id"], m["method"], m["confidence"]) == ("g1", "md5", "high")
    assert m["capture_local"] == "2023-05-01T12:00:00"


def test_md5_not_used_for_videos():
    idx = index([legacy("g1", "clip.mp4", "2023-05-01T12:00:00Z")], [("g1", "abc")])
    m = planning.match_image(img("k1", "clip.mp4", md5="abc", is_video=1), idx, "UTC")
    assert m["method"] == "filename"


def test_unique_filename_with_dims_is_medium():
    idx = index([legacy("g1", "IMG_1.PNG", "2023-05-01T12:00:00Z")])
    m = planning.match_image(img("k1", "img_1.png"), idx, "UTC")
    assert (m["method"], m["confidence"], m["notes"]) == ("filename", "medium", "dims_match")


def test_photo_dims_mismatch_is_not_a_match():
    idx = index([legacy("g1", "IMG_1.PNG", "2023-05-01T12:00:00Z", w=999, h=999)])
    m = planning.match_image(img("k1", "IMG_1.PNG"), idx, "UTC")
    assert (m["method"], m["google_id"], m["capture_local"]) == ("dims_mismatch", None, None)


def test_video_dims_mismatch_still_matches_low():
    idx = index([legacy("g1", "clip.mp4", "2023-05-01T12:00:00Z", w=3840, h=2160)])
    m = planning.match_image(img("k1", "clip.mp4", is_video=1, w=1920, h=1080), idx, "UTC")
    assert (m["google_id"], m["confidence"], m["notes"]) == ("g1", "low", "dims_mismatch")


def test_same_named_jpeg_loses_to_heic_with_matching_dims():
    idx = index([legacy("g1", "IMG_9.JPG", "2021-01-01T00:00:00Z", w=410, h=230),
                 legacy("g2", "IMG_9.HEIC", "2023-03-03T00:00:00Z", w=1576, h=2100)])
    m = planning.match_image(img("k1", "IMG_9.JPG", w=1576, h=2100), idx, "UTC")
    assert (m["google_id"], m["method"], m["confidence"]) == ("g2", "heic_basename", "medium")


def test_heic_converted_to_jpg_matches_by_stem():
    idx = index([legacy("g1", "IMG_7.HEIC", "2022-01-02T00:00:00Z")])
    m = planning.match_image(img("k1", "IMG_7.JPG"), idx, "UTC")
    assert (m["google_id"], m["method"]) == ("g1", "heic_basename")


def test_dims_disambiguate_reused_filename():
    idx = index([legacy("g1", "IMG_7.HEIC", "2019-01-01T00:00:00Z", w=4032, h=3024),
                 legacy("g2", "IMG_7.HEIC", "2023-01-01T00:00:00Z", w=100, h=50)])
    m = planning.match_image(img("k1", "IMG_7.JPG", w=50, h=100), idx, "UTC")  # rotated dims count
    assert m["google_id"] == "g2"


def test_ambiguous_same_month_still_dated():
    idx = index([legacy("g1", "a.png", "2023-05-02T00:00:00Z"), legacy("g2", "a.png", "2023-05-20T00:00:00Z")])
    m = planning.match_image(img("k1", "a.png"), idx, "UTC")
    assert m["method"] == "filename_same_month" and m["capture_local"].startswith("2023-05")


def test_ambiguous_different_months_is_undated():
    idx = index([legacy("g1", "a.png", "2019-05-02T00:00:00Z"), legacy("g2", "a.png", "2023-05-20T00:00:00Z")])
    m = planning.match_image(img("k1", "a.png"), idx, "UTC")
    assert m["method"] == "filename_ambiguous" and m["capture_local"] is None


def test_group_keeper_prefers_confidence_then_upload_time():
    images = [img("a", "x.jpg", md5="m", uploaded="2025-01-02"), img("b", "x.jpg", md5="m", uploaded="2025-01-01"),
              img("c", "y.jpg", md5="n")]
    matches = {"a": {"confidence": "high"}, "b": {"confidence": "low"}, "c": {"confidence": "none"}}
    g = planning.group_duplicates(images, matches)
    assert g["a"]["is_keeper"] == 1 and g["b"]["is_keeper"] == 0 and g["b"]["keeper_item_id"] == "a"
    assert g["c"]["group_size"] == 1 and g["c"]["is_keeper"] == 1


def test_missing_md5_never_groups_together():
    g = planning.group_duplicates([img("a", "x"), img("b", "x")], {})
    assert g["a"]["is_keeper"] == 1 and g["b"]["is_keeper"] == 1


def test_plan_targets_duplicates_and_capacity_split():
    images = [img("a", "1.jpg", md5="m1"), img("b", "1.jpg", md5="m1"),
              img("c", "2.jpg", md5="m2"), img("d", "3.jpg", md5="m3"),
              img("v", "v.mp4", md5="mv", is_video=1), img("u", "u.png", md5="mu")]
    matches = {k: {"capture_local": "2023-05-0%dT00:00:00" % i, "confidence": "high", "method": "md5"}
               for i, k in enumerate("acd", 1)}
    matches["v"] = {"capture_local": "2021-07-04T00:00:00", "confidence": "low", "method": "filename"}
    groups = planning.group_duplicates(images, matches)
    rows = {r["item_id"]: r for r in planning.plan_actions(images, matches, groups, CFG)}
    assert rows["b"]["action"] == "park_duplicate" and rows["b"]["target_name"] == "Dupes"
    assert [rows[k]["target_name"] for k in "acd"] == ["P 2023-05", "P 2023-05", "P 2023-05 - Part 2"]
    assert rows["v"]["target_name"] == "V 2021"
    assert rows["u"]["target_name"] == "P Undated"


def test_duplicate_member_dates_the_keeper():
    images = [img("a", "1.jpg", md5="m", uploaded="2025-01-01"), img("b", "1.jpg", md5="m", uploaded="2025-01-02")]
    matches = {"b": {"capture_local": "2020-02-02T00:00:00", "confidence": "medium", "method": "filename"}}
    groups = planning.group_duplicates(images, matches)
    keeper = [k for k, g in groups.items() if g["is_keeper"]][0]
    rows = {r["item_id"]: r for r in planning.plan_actions(images, matches, groups, CFG)}
    assert rows[keeper]["target_name"] == "P 2020-02"


def test_done_items_keep_their_target():
    images = [img("a", "1.jpg", md5="m1")]
    matches = {"a": {"capture_local": "2023-05-01T00:00:00", "confidence": "high", "method": "md5"}}
    groups = planning.group_duplicates(images, matches)
    rows = planning.plan_actions(images, matches, groups, CFG, existing={"a": {"status": "done", "target_name": "Old"}})
    assert rows[0]["target_name"] == "Old"
