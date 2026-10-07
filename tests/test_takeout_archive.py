"""Archive access (.zip and .tgz) and the media facts recorded by the Takeout index."""

import io
import sqlite3

import pytest
from PIL import Image

from gp2sm.media.probe import probe
from gp2sm.takeout import archive
from gp2sm.takeout import index as takeout_index
from gp2sm.takeout.items import build_items
from tests.fixtures.synthetic import jpeg_bytes, make_takeout, mp4_bytes, png_bytes, standard_takeout_entries


@pytest.mark.parametrize("name", ["takeout-1.tgz", "takeout-1.zip"])
def test_both_formats_list_and_read_the_same_members(tmp_path, name):
    entries = standard_takeout_entries()
    path = str(make_takeout(tmp_path / name, entries))
    expected = {f"Takeout/Google Photos/{rel}": data for rel, data in entries}
    members = {}
    for m in archive.iter_members(path):
        with m.open() as f:
            members[m.name] = f.read()
        assert m.size == len(members[m.name]) and m.mtime > 0
    assert members == expected
    wanted = sorted(expected)[:2]
    assert dict(archive.read_members(path, wanted)) == {k: expected[k] for k in wanted}
    assert list(archive.read_members(path, [])) == []


def test_list_archives_finds_both_formats(tmp_path):
    for n in ("b.zip", "a.tgz", "c.tar.gz", "notes.txt"):
        (tmp_path / n).write_bytes(b"")
    assert archive.list_archives(tmp_path) == ["a.tgz", "b.zip", "c.tar.gz"]


def test_probe_still_with_offset_and_without():
    info = probe(jpeg_bytes(w=40, h=30, exif_dt="2023:07:04 10:00:00", exif_offset="-04:00"), ".jpg")
    assert info == {"width": 40, "height": 30, "duration_s": None, "own_time": "2023-07-04T10:00:00-04:00"}
    assert probe(jpeg_bytes(exif_dt="2023:07:04 10:00:00"), ".JPG")["own_time"] == "2023-07-04T10:00:00"
    assert probe(png_bytes(), ".png")["own_time"] is None
    assert probe(jpeg_bytes(exif_dt="0000:00:00 00:00:00"), ".jpg")["own_time"] is None


def test_probe_heic():
    pytest.importorskip("pillow_heif")
    buf = io.BytesIO()
    img = Image.new("RGB", (48, 64), (1, 2, 3))
    exif = img.getexif()
    exif.get_ifd(0x8769)[0x9003] = "2024:08:12 23:58:19"
    exif.get_ifd(0x8769)[0x9011] = "-04:00"
    img.save(buf, "HEIF", exif=exif)
    assert probe(buf.getvalue(), ".HEIC") == {"width": 48, "height": 64, "duration_s": None,
                                              "own_time": "2024-08-12T23:58:19-04:00"}


def test_probe_clip_prefers_apple_date():
    info = probe(mp4_bytes(duration_s=3.0, w=1440, h=1080, created_unix=2_000_000_000,
                           apple_date="2023-07-04T10:00:00-0400"), ".MOV")
    assert (info["width"], info["height"], round(info["duration_s"], 2)) == (1440, 1080, 3.0)
    assert info["own_time"] == "2023-07-04T14:00:00+00:00"


def _index(tmp_path, *names_entries):
    paths = [str(make_takeout(tmp_path / n, e)) for n, e in names_entries]
    db = tmp_path / "idx.db"
    takeout_index.main(paths + ["--db", str(db)])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.parametrize("fmt", ["tgz", "zip"])
def test_index_records_media_facts_and_pairs_items(tmp_path, fmt):
    entries = standard_takeout_entries() + [
        ("Photos from 2023/IMG_0002.JPG", jpeg_bytes(w=40, h=30, exif_dt="2023:07:04 10:00:00", exif_offset="-04:00")),
        ("Photos from 2023/broken.jpg", b"not an image"),
    ]
    clip = [e for e in entries if e[0].endswith("IMG_0001.MP4")]
    rest = [e for e in entries if not e[0].endswith("IMG_0001.MP4")]
    idx = _index(tmp_path, (f"takeout-1.{fmt}", rest), (f"takeout-2.{fmt}", clip))
    rows = {r["basename"]: r for r in idx.execute("SELECT * FROM members")}
    assert (rows["IMG_0002.JPG"]["width"], rows["IMG_0002.JPG"]["own_time"]) == (40, "2023-07-04T10:00:00-04:00")
    assert rows["IMG_0001.MP4"]["width"] == 1920 and rows["IMG_0001.MP4"]["own_time"].startswith("2023-07-04T14:00")
    assert rows["Screenshot.PNG"]["width"] == 32
    assert rows["broken.jpg"]["probe_error"] and rows["broken.jpg"]["width"] is None   # recorded, not fatal
    assert rows["IMG_0001.HEIC.supplemental-metadata.json"]["width"] is None
    items, _ = build_items(idx)
    still = next(it for it in items if it["media"] and it["media"]["basename"] == "IMG_0001.HEIC")
    assert still["motion"]["archive"] == f"takeout-2.{fmt}"


def test_large_video_is_probed_from_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(takeout_index, "FULL_READ_MAX", 1000)
    monkeypatch.setattr(takeout_index, "EDGE", 600)
    data = mp4_bytes(duration_s=5.0, w=1280, h=720, pad=5000)   # boxes at the start, padding after
    path = str(make_takeout(tmp_path / "t.zip", [("Photos from 2023/big.mp4", data)]))
    _, rows, _ = takeout_index.index_archive(path)
    (row,) = rows
    assert row[2] == len(data) and row[9:12] == (1280, 720, 5.0)


def test_old_index_is_upgraded_in_place(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE members(archive TEXT, path TEXT, size INT, mtime INT, md5 TEXT, ext TEXT, folder TEXT, "
               "basename TEXT, json TEXT, PRIMARY KEY(archive, path))")
    db.execute("INSERT INTO members VALUES('a.tgz', 'x.jpg', 1, 1, 'm', '.jpg', '', 'x.jpg', NULL)")
    db.commit()
    db.close()
    path = str(make_takeout(tmp_path / "b.zip", [("Photos from 2023/y.png", png_bytes())]))
    takeout_index.main([path, "--db", str(tmp_path / "old.db")])
    conn = sqlite3.connect(tmp_path / "old.db")
    rows = dict(conn.execute("SELECT basename, width FROM members"))
    assert rows == {"x.jpg": None, "y.png": 32}
