import hashlib
import sqlite3

from gp2sm.media import aspect, mp4_dims, mp4_duration
from gp2sm.media.mp4 import clip_creation_ts
from gp2sm.takeout import index as takeout_index
from gp2sm.takeout.match import build_items
from tests.fixtures.synthetic import jpeg_bytes, make_takeout, mp4_bytes, standard_takeout_entries


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def test_generators_are_deterministic(tmp_path):
    a = make_takeout(tmp_path / "a.tgz", standard_takeout_entries())
    b = make_takeout(tmp_path / "b.tgz", standard_takeout_entries())
    assert md5(a) == md5(b)
    assert jpeg_bytes(exif_dt="2023:07:04 10:00:00") == jpeg_bytes(exif_dt="2023:07:04 10:00:00")


def test_mp4_fixture_round_trips_through_parsers():
    data = mp4_bytes(duration_s=12.5, w=1080, h=1920, created_unix=1_688_479_200)
    assert abs(mp4_duration(data) - 12.5) < 0.01
    assert mp4_dims(data) == (1080, 1920) and round(aspect(*mp4_dims(data)), 3) == round(1920 / 1080, 3)
    assert clip_creation_ts(data) == 1_688_479_200
    apple = mp4_bytes(created_unix=2_000_000_000, apple_date="2023-07-04T10:00:00-0400")
    assert clip_creation_ts(apple) == 1_688_479_200  # Apple's tag wins over a later re-encode time


def _index(tmp_path, *archives):
    db = tmp_path / "idx.db"
    takeout_index.main([str(a) for a in archives] + ["--db", str(db)])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def test_takeout_index_and_pairing_end_to_end(tmp_path):
    arch = make_takeout(tmp_path / "takeout-1.tgz", standard_takeout_entries())
    idx = _index(tmp_path, arch)
    assert idx.execute("SELECT COUNT(*) FROM members").fetchone()[0] == len(standard_takeout_entries())
    items, orphans = build_items(idx)
    paired = {it["media"]["basename"]: it for it in items if it["media"]}
    # sidecar -> media, including Google's "(N)" counter on the sidecar name
    assert set(paired) == {"IMG_0001.HEIC", "lp_image.heic", "lp_image(1).heic", "Screenshot.PNG"}
    # Live Photo clip attached to its still by name
    assert paired["IMG_0001.HEIC"]["motion"]["basename"] == "IMG_0001.MP4"
    # a clip with no still is an orphan, not silently dropped
    assert [o["basename"] for o in orphans] == ["IMG_0999.MP4"]


def test_live_pair_split_across_archives_is_indexed_in_both(tmp_path):
    entries = standard_takeout_entries()
    clip = [e for e in entries if e[0].endswith("IMG_0001.MP4")]
    rest = [e for e in entries if not e[0].endswith("IMG_0001.MP4")]
    a = make_takeout(tmp_path / "takeout-1.tgz", rest)
    b = make_takeout(tmp_path / "takeout-2.tgz", clip)
    idx = _index(tmp_path, a, b)
    items, _ = build_items(idx)
    still = next(it for it in items if it["media"] and it["media"]["basename"] == "IMG_0001.HEIC")
    assert still["media"]["archive"] == "takeout-1.tgz"
    assert still["motion"]["archive"] == "takeout-2.tgz"  # the clip's own archive is recorded
