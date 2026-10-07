"""The import planner (pure): targets, names, conversions, clip pairing, review holds, caps."""

import datetime

from gp2sm.importer.plan import Albums, landed_name, plan
from gp2sm.smugmug.client import SMUGMUG_CAPABILITIES as CAPS

TZ = "America/New_York"
ALBUMS = {"photo": "Photos {yyyy}-{mm}", "video": "Videos {yyyy}-{mm}", "undated_photo": "Photos Undated",
          "undated_video": "Videos Undated"}
POLICY = {"heic": "convert", "live_clips": "pair", "unpaired_clips": "dated", "rejected_types": "convert",
          "pair_window": 60, "aspect_tolerance_pct": 2}
T = int(datetime.datetime(2023, 7, 4, 18, 0, tzinfo=datetime.timezone.utc).timestamp())   # 14:00 New York


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def photo(ref, name, ts=T, w=4032, h=3024, own=True, motion=None):
    return {"ref": ref, "name": name, "kind": "photo", "taken_ts": ts, "own_time": iso(ts) if own else None,
            "width": w, "height": h, "duration_s": None, "orphan": False, "motion": motion}


def clip(ref, name, ts=T, w=1440, h=1080, dur=2.5):
    return {"ref": ref, "name": name, "own_time": iso(ts) if ts else None, "width": w, "height": h, "duration_s": dur}


def video(ref, name, ts=T, dur=30.0, orphan=False):
    return {"ref": ref, "name": name, "kind": "video", "taken_ts": None if orphan else ts, "own_time": iso(ts),
            "width": 1920, "height": 1080, "duration_s": dur, "orphan": orphan, "motion": None}


def EMPTY():
    return {"items": {}, "names": {}, "counts": {}}


def run(items, decisions=None, dest=None, previous=None, policy=None, soft_cap=4000):
    return plan(items, decisions or {}, dest or EMPTY(), previous or {}, dict(POLICY, **(policy or {})), ALBUMS, TZ,
                CAPS, soft_cap)


def by_role(rows):
    return {(r["source_ref"], r["role"]): r for r in rows}


def test_live_photo_pair_lands_together_converted():
    rows, _ = run([photo("p1", "IMG_1.HEIC", motion=clip("c1", "IMG_1.MP4", ts=T + 1))])
    r = by_role(rows)
    assert (r["p1", "still"]["target_name"], r["p1", "still"]["upload_name"], r["p1", "still"]["convert"]) == (
        "Photos 2023-07", "IMG_1.JPG", True)
    assert (r["c1", "clip"]["target_name"], r["c1", "clip"]["upload_name"], r["c1", "clip"]["pair_ref"]) == (
        "Photos 2023-07", "IMG_1.MP4", "p1")
    assert r["c1", "clip"]["content_type"] == "video/mp4" and r["p1", "still"]["content_type"] == "image/jpeg"


def test_clips_pair_by_time_not_name():
    # names were swapped by the source: each clip's own time matches the *other* still
    a = photo("pa", "lp_image.heic", ts=T, motion=clip("ca", "lp_image.mp4", ts=T + 300))
    b = photo("pb", "lp_image(1).heic", ts=T + 300, motion=clip("cb", "lp_image(1).mp4", ts=T))
    r = by_role(run([a, b])[0])
    assert r["ca", "clip"]["pair_ref"] == "pb" and r["cb", "clip"]["pair_ref"] == "pa"
    assert r["ca", "clip"]["upload_name"] == "lp_image(1).MP4"   # named after the still it really belongs to
    assert "paired by time" in r["ca", "clip"]["reason"]


def test_same_name_collisions_get_unique_names():
    dest = {"items": {}, "names": {"Photos 2023-07": {"img_1.jpg"}}, "counts": {"Photos 2023-07": 1}}
    rows, _ = run([photo("p1", "IMG_1.HEIC", motion=clip("c1", "IMG_1.MP4"))], dest=dest)
    r = by_role(rows)
    assert r["p1", "still"]["upload_name"] == "IMG_1 (1).JPG" and r["c1", "clip"]["upload_name"] == "IMG_1 (1).MP4"


def test_clip_next_to_a_still_already_on_the_destination():
    dest = {"items": {"D1": {"album": "Old Album", "name": "IMG_2.JPG", "is_video": False}},
            "names": {"Old Album": {"img_2.jpg"}}, "counts": {"Old Album": 1}}
    items = [photo("p2", "IMG_2.HEIC", motion=clip("c2", "IMG_2.MP4"))]
    rows, notes = run(items, decisions={"p2": {"decision": "same", "dest_item_id": "D1"}}, dest=dest)
    assert [(r["role"], r["target_name"], r["upload_name"]) for r in rows] == [("clip", "Old Album", "IMG_2.MP4")]
    assert notes == {"already on the destination (same)": 1}
    dest["names"]["Old Album"].add("img_2.mp4")    # ... and once it's there, it isn't planned again
    rows, notes = run(items, decisions={"p2": {"decision": "same", "dest_item_id": "D1"}}, dest=dest)
    assert rows == [] and notes["clip already beside its still"] == 1


def test_review_holds_the_still_and_its_clip():
    rows, _ = run([photo("p1", "IMG_1.HEIC", motion=clip("c1", "IMG_1.MP4"))],
                  decisions={"p1": {"decision": "review", "dest_item_id": "D9"}})
    assert {r["status"] for r in rows} == {"needs_review"}


def test_policies_heic_keep_rejected_types_and_clip_modes():
    items = [photo("p1", "IMG_1.HEIC", motion=clip("c1", "IMG_1.MP4")), photo("w", "pic.webp", own=False)]
    r = by_role(run(items, policy={"heic": "keep", "rejected_types": "skip", "live_clips": "separate"})[0])
    assert (r["p1", "still"]["upload_name"], r["p1", "still"]["convert"]) == ("IMG_1.HEIC", False)
    assert ("w", "still") not in r
    assert r["c1", "clip"]["target_name"] == "Videos 2023-07"
    r = by_role(run(items)[0])
    assert (r["w", "still"]["upload_name"], r["w", "still"]["convert"]) == ("pic.JPG", True)
    rows, notes = run(items, policy={"live_clips": "skip"})
    assert {x["role"] for x in rows} == {"still"} and notes["Live Photo clips skipped (live_clips = skip)"] == 1


def test_kept_heic_cannot_collide_with_the_jpg_it_becomes():
    assert landed_name("IMG_1.HEIC", CAPS) == "IMG_1.JPG"
    dest = {"items": {}, "names": {"Photos 2023-07": {"img_1.jpg"}}, "counts": {}}
    r = by_role(run([photo("p1", "IMG_1.HEIC")], dest=dest, policy={"heic": "keep"})[0])
    assert r["p1", "still"]["upload_name"] == "IMG_1 (1).HEIC"


def test_videos_unpaired_clips_and_undated():
    items = [video("v1", "Birthday.MP4"), video("o1", "IMG_9.MOV", ts=T + 3600, dur=2.0, orphan=True),
             photo("p0", "scan.jpg", ts=None, own=False)]
    r = by_role(run(items)[0])
    assert r["v1", "video"]["target_name"] == "Videos 2023-07"
    assert (r["o1", "clip"]["target_name"], r["o1", "clip"]["reason"]) == ("Videos 2023-07", "clip with no matching still")
    assert r["p0", "still"]["target_name"] == "Photos Undated"
    r = by_role(run(items, policy={"unpaired_clips": "undated"})[0])
    assert r["o1", "clip"]["target_name"] == "Videos Undated"
    assert ("o1", "clip") not in by_role(run(items, policy={"unpaired_clips": "skip"})[0])


def test_orphan_short_video_pairs_with_a_still_by_time():
    items = [photo("p1", "IMG_1.HEIC"), video("o1", "random.MOV", ts=T + 1, dur=2.0, orphan=True)]
    items[1]["width"], items[1]["height"] = 1440, 1080
    r = by_role(run(items)[0])
    assert r["o1", "clip"]["upload_name"] == "IMG_1.MOV" and r["o1", "clip"]["pair_ref"] == "p1"


def test_source_duplicates_and_previous_rows_are_not_replanned():
    items = [photo("p1", "IMG_1.HEIC", motion=clip("c1", "IMG_1.MP4")), photo("p1b", "IMG_1.HEIC")]
    previous = {("p1", "still"): {"target_name": "Photos 2023-07", "upload_name": "IMG_1.JPG"}}
    rows, notes = run(items, decisions={"p1b": {"decision": "source_duplicate"}}, previous=previous)
    assert [(r["source_ref"], r["role"], r["upload_name"]) for r in rows] == [("c1", "clip", "IMG_1.MP4")]
    assert notes == {"same file appears twice in the source": 1}


def test_soft_cap_continues_in_parts():
    albums = Albums({}, {"Photos 2023-07": 2}, soft_cap=2)
    assert albums.part_for("Photos 2023-07") == "Photos 2023-07 - Part 2"
    rows, _ = run([photo(f"p{i}", f"IMG_{i}.JPG") for i in range(3)], soft_cap=2)
    assert [r["target_name"] for r in rows] == ["Photos 2023-07", "Photos 2023-07", "Photos 2023-07 - Part 2"]
