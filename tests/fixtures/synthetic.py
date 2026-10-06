"""Deterministic synthetic test data: no real photos, names, ids or account data.

- jpeg_bytes/png_bytes: small images, optional EXIF capture date
- mp4_bytes: just enough MP4 structure for header parsers (mvhd/tkhd, optional Apple creationdate)
- make_takeout: a Google-Takeout-shaped .tgz (Photos from YYYY/ folders, supplemental-metadata sidecars,
  '(N)' collision names, Live Photo pairs, optionally a pair split across two archives)
"""

import datetime
import gzip
import io
import json
import tarfile

from PIL import Image

QT_EPOCH_OFFSET = 2082844800
FIXED_MTIME = 1_700_000_000


def jpeg_bytes(w=64, h=48, color=(120, 80, 40), exif_dt=None):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    if exif_dt:
        exif = img.getexif()
        exif.get_ifd(0x8769)[0x9003] = exif_dt  # DateTimeOriginal "YYYY:MM:DD HH:MM:SS"
        img.save(buf, "JPEG", quality=90, exif=exif)
    else:
        img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def png_bytes(w=32, h=32, color=(10, 200, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def mp4_bytes(duration_s=2.0, w=1920, h=1080, created_unix=FIXED_MTIME, apple_date=None, pad=0):
    """Minimal box layout: ftyp + moov(mvhd v0, audio tkhd 0x0, video tkhd WxH) [+ Apple creationdate]."""
    timescale = 600
    mvhd = (b"mvhd" + bytes([0, 0, 0, 0]) + (created_unix + QT_EPOCH_OFFSET).to_bytes(4, "big") + b"\0" * 4
            + timescale.to_bytes(4, "big") + int(duration_s * timescale).to_bytes(4, "big") + b"\0" * 80)

    def tkhd(tw, th):
        return (b"tkhd" + bytes([0, 0, 0, 0]) + b"\0" * 20 + b"\0" * 8 + b"\0" * 8 + b"\0" * 36
                + (tw << 16).to_bytes(4, "big") + (th << 16).to_bytes(4, "big"))

    meta = b""
    if apple_date:  # e.g. "2023-07-04T10:00:00-0400"
        meta = b"keys....com.apple.quicktime.creationdate....data" + apple_date.encode()
    return b"\0\0\0\x18ftypmp42" + b"\0" * 12 + mvhd + tkhd(0, 0) + tkhd(w, h) + meta + b"\0" * pad


def sidecar(title, taken_unix, created_unix=None):
    return json.dumps({
        "title": title,
        "photoTakenTime": {"timestamp": str(taken_unix)},
        "creationTime": {"timestamp": str(created_unix or taken_unix + 3600)},
        "geoData": {"latitude": 0.0, "longitude": 0.0},
        "url": f"https://photos.google.com/photo/SYNTHETIC-{title}",
    }, indent=2).encode()


def _add(tar, path, data):
    info = tarfile.TarInfo(path)
    info.size = len(data)
    info.mtime = FIXED_MTIME
    tar.addfile(info, io.BytesIO(data))


def make_takeout(path, entries):
    """Write a deterministic .tgz. entries: list of (relative path under 'Takeout/Google Photos/', bytes)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for rel, data in entries:
            _add(tar, f"Takeout/Google Photos/{rel}", data)
    with open(path, "wb") as f, gzip.GzipFile(filename="", fileobj=f, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return path


def standard_takeout_entries(year=2023):
    """A small, varied library: a Live Photo pair, a '(N)' collision pair, a PNG, and an unpaired clip."""
    t = int(datetime.datetime(year, 7, 4, 14, 0, tzinfo=datetime.timezone.utc).timestamp())
    d = f"Photos from {year}"
    return [
        (f"{d}/IMG_0001.HEIC", jpeg_bytes(color=(1, 2, 3))),        # stand-in bytes; parsers don't decode HEIC here
        (f"{d}/IMG_0001.HEIC.supplemental-metadata.json", sidecar("IMG_0001.HEIC", t)),
        (f"{d}/IMG_0001.MP4", mp4_bytes(created_unix=t)),           # its Live Photo motion clip
        (f"{d}/lp_image.heic", jpeg_bytes(color=(4, 5, 6))),
        (f"{d}/lp_image.heic.supplemental-metadata.json", sidecar("lp_image.heic", t + 60)),
        (f"{d}/lp_image(1).heic", jpeg_bytes(color=(7, 8, 9))),
        (f"{d}/lp_image.heic.supplemental-metadata(1).json", sidecar("lp_image.heic", t + 61)),
        (f"{d}/Screenshot.PNG", png_bytes()),
        (f"{d}/Screenshot.PNG.supplemental-metadata.json", sidecar("Screenshot.PNG", t + 120)),
        (f"{d}/IMG_0999.MP4", mp4_bytes(created_unix=t + 500)),      # motion clip with no still
    ]
