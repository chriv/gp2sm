"""Dedupe rules (pure) and the inventory + hashing glue against the fake destination."""

import pytest

from gp2sm.importer import dedupe, inventory
from gp2sm.media.phash import bytes_hash
from gp2sm.state import State
from tests.fakes.smugmug import FakeSmugMug
from tests.fixtures.synthetic import jpeg_bytes


def src(ref, name, kind="photo", md5=None, **kw):
    return {"ref": ref, "name": name, "kind": kind, "md5": md5 or f"md5-{ref}", **kw}


def dst(item_id, name, md5=None, is_video=False, album="A", **kw):
    return {"item_id": item_id, "album_id": album, "name": name, "md5": md5, "is_video": is_video, **kw}


def H(bits):
    """A 4-rotation source hash whose 0-degree hash is `bits` (others far away)."""
    return [bits, ~bits & (2**64 - 1), 0, 2**64 - 1]


def decisions(out):
    return {ref: d["decision"] for ref, d in out.items()}


def test_group_key():
    assert dedupe.group_key("IMG_1(2).HEIC") == dedupe.group_key("img_1.JPG") == dedupe.group_key("IMG_1 (3).jpg")
    assert dedupe.group_key("IMG_12.jpg") != dedupe.group_key("IMG_1.jpg")


def test_exact_md5_and_source_duplicates():
    out = dedupe.decide([src("y/a", "a.jpg", md5="m1"), src("z/a", "a.jpg", md5="m1"), src("y/b", "b.jpg")],
                        [dst("D1", "whatever.jpg", md5="m1")], {}, {})
    assert decisions(out) == {"y/a": "exact", "z/a": "source_duplicate", "y/b": "new"}
    assert out["y/a"]["dest_item_id"] == "D1" and out["z/a"]["detail"] == "same file as y/a"


def test_content_bands_same_new_review():
    s = [src("1", "IMG_1.HEIC"), src("2", "IMG_2.HEIC"), src("3", "IMG_3.HEIC"), src("4", "IMG_4.HEIC")]
    d = [dst("D1", "IMG_1.JPG"), dst("D2", "IMG_2.JPG"), dst("D3", "IMG_3.JPG")]
    sh = {"1": H(0), "2": H(0), "3": H(0)}
    dh = {"D1": 0b11, "D2": (1 << 25) - 1, "D3": (1 << 10) - 1}   # distances 2, 25, 10
    out = dedupe.decide(s, d, sh, dh)
    assert decisions(out) == {"1": "same", "2": "new", "3": "review", "4": "new"}
    assert out["1"]["dist"] == 2 and out["3"]["dest_item_id"] == "D3" and out["4"]["method"] == "none"


def test_bursts_pair_one_to_one_and_extras_are_new():
    s = [src(f"b{i}", f"lp_image({i}).heic") for i in range(3)]
    d = [dst("D0", "lp_image.JPG"), dst("D1", "lp_image (1).JPG")]
    sh = {"b0": H(0), "b1": H(1), "b2": H(3)}
    dh = {"D0": 0, "D1": 1}
    out = dedupe.decide(s, d, sh, dh)
    assert sorted((ref, o["dest_item_id"]) for ref, o in out.items() if o["decision"] == "same") == [
        ("b0", "D0"), ("b1", "D1")]
    assert out["b2"]["decision"] == "new" and "claimed" in out["b2"]["detail"]


def test_unreadable_hash_goes_to_review_not_new():
    out = dedupe.decide([src("1", "IMG_1.HEIC")], [dst("D1", "IMG_1.JPG")], {"1": None}, {"D1": 0})
    assert out["1"]["decision"] == "review"


def test_videos_by_shape():
    s = [src("v1", "IMG_5.MOV", kind="video", duration_s=10.0, width=1920, height=1080),
         src("v2", "IMG_6.MOV", kind="video", duration_s=10.0, width=1920, height=1080),
         src("v3", "IMG_7.MOV", kind="video", duration_s=None)]
    d = [dst("E1", "IMG_5.MP4", is_video=True, duration_s=10.3, width=1280, height=720),
         dst("E2", "IMG_6.MP4", is_video=True, duration_s=30.0, width=1920, height=1080),
         dst("E3", "IMG_7.MP4", is_video=True, duration_s=5.0)]
    assert decisions(dedupe.decide(s, d, {}, {})) == {"v1": "same", "v2": "new", "v3": "review"}


def test_modes_exact_and_off():
    s = [src("1", "IMG_1.HEIC", md5="m"), src("2", "IMG_2.HEIC")]
    d = [dst("D1", "x.jpg", md5="m"), dst("D2", "IMG_2.JPG")]
    assert decisions(dedupe.decide(s, d, {}, {}, mode="exact")) == {"1": "exact", "2": "new"}
    assert decisions(dedupe.decide(s, d, {}, {}, mode="off")) == {"1": "new", "2": "new"}


def test_review_answer_overrides():
    assert dedupe.effective_decision("review", "same") == "same"
    assert dedupe.effective_decision("review", "different") == "new"
    assert dedupe.effective_decision("review", None) == "review"


@pytest.fixture
def st(tmp_path):
    return State(str(tmp_path / "s.db"))


def test_inventory_and_run_end_to_end(st, tmp_path):
    fake = FakeSmugMug()
    family = fake.ensure_folder_path(fake.root_folder(), "Family")
    other = fake.ensure_folder_path(fake.root_folder(), "Other")
    a = fake.ensure_album(family, "2023-07")[0]
    b = fake.ensure_album(other, "Elsewhere")[0]
    same_pic = jpeg_bytes(w=64, h=48, color=(200, 30, 30))
    (tmp_path / "IMG_1.JPG").write_bytes(same_pic)
    up = fake.upload_file(a, str(tmp_path / "IMG_1.JPG"), "IMG_1.JPG", "image/jpeg")
    fake.items[up["item_id"]]["preview"] = same_pic
    (tmp_path / "exact.jpg").write_bytes(b"exact bytes")
    fake.upload_file(b, str(tmp_path / "exact.jpg"), "exact.jpg", "image/jpeg")

    assert inventory.refresh(st, fake, ["Family"]) == {"albums": 1, "items": 1, "albums_dropped": 0}
    summary = inventory.refresh(st, fake, ["/"])
    assert summary["albums"] == 2 and summary["items"] == 2

    import hashlib
    sources = [src("t::IMG_1.HEIC", "IMG_1.HEIC"), src("t::copy.jpg", "copy.jpg", md5=hashlib.md5(b"exact bytes").hexdigest()),
               src("t::new.jpg", "new.jpg")]
    reads = []

    def read_refs(refs):
        reads.extend(refs)
        for r in refs:
            yield r, same_pic
    policy = {"dedupe": "content", "same_max": 6, "different_min": 19, "aspect_tolerance_pct": 2}
    assert dedupe.run(st, fake, sources, read_refs, policy) == {"exact": 1, "new": 1, "same": 1}
    assert reads == ["t::IMG_1.HEIC"]                      # only photos with a same-named candidate are read
    assert st.one("SELECT h FROM hash_dest") == f"{bytes_hash(same_pic):016x}"
    dedupe.run(st, fake, sources, read_refs, policy)
    assert reads == ["t::IMG_1.HEIC"]                      # hashes are cached
    st.db.execute("UPDATE source_matches SET reviewed='different' WHERE source_ref='t::IMG_1.HEIC'")
    st.db.commit()
    dedupe.run(st, fake, sources, read_refs, policy)        # re-deciding keeps the person's answer
    assert st.one("SELECT reviewed FROM source_matches WHERE source_ref='t::IMG_1.HEIC'") == "different"


def test_video_durations_are_fetched_only_for_same_named_candidates(st):
    fake = FakeSmugMug(albums={"A": {"V1", "V2"}},
                       items={"V1": {"name": "IMG_5.MP4", "is_video": True, "duration_s": 10.1, "width": 1920,
                                     "height": 1080},
                              "V2": {"name": "other.MP4", "is_video": True, "duration_s": 3.0}})
    fake.names["A"] = "Album"
    inventory.refresh(st, fake, ["/"])
    for item in fake.items.values():
        item.pop("duration_s")          # listings don't carry durations; video_info does
    fake.items["V1"]["duration_s"] = 10.1
    assert st.one("SELECT duration_s FROM dest_items WHERE item_id='V1'") is not None   # (fake listing had it)
    st.db.execute("UPDATE dest_items SET duration_s=NULL")
    st.db.commit()
    sources = [src("t::IMG_5.MOV", "IMG_5.MOV", kind="video", duration_s=10.0, width=1920, height=1080)]
    policy = {"dedupe": "content", "same_max": 6, "different_min": 19, "aspect_tolerance_pct": 2}
    assert dedupe.run(st, fake, sources, lambda refs: iter(()), policy) == {"same": 1}
    assert dict(st.q("SELECT item_id, duration_s FROM dest_items")) == {"V1": 10.1, "V2": None}


def test_bounded_map_limits_work_in_flight():
    import concurrent.futures
    import threading
    gate = threading.Event()
    produced = []

    def pairs():
        for i in range(10):
            produced.append(i)
            yield i, i

    def slow(x):
        gate.wait(5)
        return x * 2

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        it = dedupe.bounded_map(pool, slow, pairs(), limit=3)
        t = threading.Timer(0.2, gate.set)
        t.start()
        first = next(it)
        assert len(produced) <= 4          # the producer waited instead of reading everything ahead
        rest = list(it)
    assert sorted([first[0]] + [k for k, _ in rest]) == list(range(10))
    assert all(f.result() == k * 2 for k, f in [first] + rest)
