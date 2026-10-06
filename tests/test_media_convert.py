"""HEIC -> JPEG conversion keeps the original EXIF exactly (except Orientation, reset because pixels are oriented)."""

import io

import piexif
import pytest
from PIL import Image

from gp2sm.media.convert import render_small, to_jpeg

pillow_heif = pytest.importorskip("pillow_heif")


def heic_with_exif(orientation=1, w=64, h=48):
    exif = {
        "0th": {piexif.ImageIFD.Make: b"Apple", piexif.ImageIFD.Model: b"iPhone 14", piexif.ImageIFD.Orientation: orientation},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2024:08:12 23:58:19", piexif.ExifIFD.OffsetTimeOriginal: b"-04:00",
                 piexif.ExifIFD.LensSpecification: ((1543, 1000), (6, 1), (12, 5), (12, 5))},
        "GPS": {piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: ((35, 1), (12, 1), (3017, 100)),
                piexif.GPSIFD.GPSLongitudeRef: b"W", piexif.GPSIFD.GPSLongitude: ((80, 1), (38, 1), (5339, 100)),
                piexif.GPSIFD.GPSTimeStamp: ((3, 1), (58, 1), (19, 1))},
    }
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 120, 200)).save(buf, "HEIF", exif=piexif.dump(exif), quality=90)
    return buf.getvalue()


def tags(e):
    return {(i, k): v for i in ("0th", "Exif", "GPS") for k, v in e[i].items()
            if (i, k) not in {("0th", piexif.ImageIFD.Orientation), ("0th", piexif.ImageIFD.ExifTag),
                              ("0th", piexif.ImageIFD.GPSTag)}}


@pytest.mark.parametrize("orientation", [1, 6])
def test_heic_to_jpeg_preserves_all_exif(tmp_path, orientation):
    data = heic_with_exif(orientation)
    raw = Image.open(io.BytesIO(data)).info["exif"]
    original = piexif.load(raw[6:] if raw.startswith(b"Exif\x00\x00") else raw)
    out = tmp_path / "out.jpg"
    to_jpeg(data, ".heic", str(out))
    converted = piexif.load(str(out))
    assert tags(converted) == tags(original)                      # every tag, every value, nothing added
    assert converted["0th"].get(piexif.ImageIFD.Orientation, 1) == 1
    assert converted["GPS"][piexif.GPSIFD.GPSTimeStamp] == ((3, 1), (58, 1), (19, 1))


def test_jpeg_input_passes_exif_through(tmp_path):
    buf = io.BytesIO()
    exif = piexif.dump({"0th": {piexif.ImageIFD.Make: b"X"}, "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2020:01:02 03:04:05"}})
    Image.new("RGB", (20, 10)).save(buf, "JPEG", exif=exif)
    out = tmp_path / "o.jpg"
    to_jpeg(buf.getvalue(), ".jpg", str(out))
    assert piexif.load(str(out))["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2020:01:02 03:04:05"


def test_render_small_bounds_size():
    img = render_small(heic_with_exif(w=800, h=600), max_side=200)
    assert max(img.size) == 200 and img.mode == "RGB"
