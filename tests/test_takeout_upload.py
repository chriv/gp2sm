import piexif
from PIL import Image

from gp2sm.takeout.upload import exif_datetime_fields, fill_exif_date


def test_exif_fields_use_local_time_and_offset():
    # 2024-08-13T04:03:05Z is 00:03:05 EDT
    assert exif_datetime_fields(1723521785, "America/New_York") == ("2024:08:13 00:03:05", "-04:00")
    # winter: EST
    assert exif_datetime_fields(1704067200, "America/New_York")[1] == "-05:00"


def test_fill_exif_date_only_when_missing(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(p)
    note = fill_exif_date(str(p), 1723521785, "America/New_York")
    assert note.startswith("date filled")
    exif = piexif.load(str(p))
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2024:08:13 00:03:05"
    assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"-04:00"
    # second call keeps the existing (camera) date
    assert fill_exif_date(str(p), 0, "UTC") == "camera date kept"


def test_fill_exif_date_lossless(tmp_path):
    p = tmp_path / "b.jpg"
    Image.new("RGB", (32, 32), (10, 200, 30)).save(p, quality=90)
    before = Image.open(p).tobytes()
    fill_exif_date(str(p), 1723521785, "UTC")
    assert Image.open(p).tobytes() == before  # pixels untouched


def test_mp4_creation_ts_v0_and_v1():
    from gp2sm.media.mp4 import QT_EPOCH_OFFSET, mp4_creation_ts
    from tests.fixtures.synthetic import _box
    unix = 1723521785
    v0 = _box(b"moov", _box(b"mvhd", bytes([0, 0, 0, 0]) + (unix + QT_EPOCH_OFFSET).to_bytes(4, "big") + bytes(16)))
    v1 = _box(b"moov", _box(b"mvhd", bytes([1, 0, 0, 0]) + (unix + QT_EPOCH_OFFSET).to_bytes(8, "big") + bytes(24)))
    assert mp4_creation_ts(v0) == unix
    assert mp4_creation_ts(v1) == unix
    assert mp4_creation_ts(b"no movie header here") is None


def test_clip_creation_prefers_apple_date_over_mvhd():
    from gp2sm.media.mp4 import QT_EPOCH_OFFSET, clip_creation_ts
    from tests.fixtures.synthetic import _box
    mvhd = _box(b"mvhd", bytes([0, 0, 0, 0]) + (2000000000 + QT_EPOCH_OFFSET).to_bytes(4, "big") + bytes(16))
    meta = _box(b"meta", b"keys...com.apple.quicktime.creationdate...data2022-06-14T21:22:49-0400...")
    assert clip_creation_ts(_box(b"moov", mvhd + meta)) == 1655256169  # 2022-06-15T01:22:49Z
    assert clip_creation_ts(_box(b"moov", mvhd)) == 2000000000  # no Apple tag: fall back to mvhd
