"""Deterministic synthetic test data: no real photos, names, ids or account data.

- jpeg_bytes/png_bytes: small images, optional EXIF capture date; pattern_jpeg: distinct pictures for dHash
- mp4_bytes: just enough MP4 structure for header parsers (mvhd/tkhd, optional Apple creationdate)
- make_takeout: a Google-Takeout-shaped .tgz or .zip (Photos from YYYY/ folders, supplemental-metadata sidecars,
  '(N)' collision names, Live Photo pairs, optionally a pair split across two archives)
"""

import datetime
import gzip
import io
import json
import tarfile
import zipfile

from PIL import Image

QT_EPOCH_OFFSET = 2082844800
FIXED_MTIME = 1_700_000_000


def jpeg_bytes(w=64, h=48, color=(120, 80, 40), exif_dt=None, exif_offset=None):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    if exif_dt:
        exif = img.getexif()
        exif.get_ifd(0x8769)[0x9003] = exif_dt  # DateTimeOriginal "YYYY:MM:DD HH:MM:SS"
        if exif_offset:
            exif.get_ifd(0x8769)[0x9011] = exif_offset  # OffsetTimeOriginal "+HH:MM"
        img.save(buf, "JPEG", quality=90, exif=exif)
    else:
        img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def pattern_jpeg(seed, w=96, h=72):
    """A JPEG with a seed-dependent pattern, so perceptual hashes tell different pictures apart."""
    img = Image.new("RGB", (w, h), (128, 128, 128))
    px = img.load()
    for y in range(h):
        for x in range(w):
            v = ((x * (seed + 3) + y * (2 * seed + 5)) // 7 + seed * 37) % 256
            px[x, y] = (v, (v * 3 + seed) % 256, 255 - v)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def png_bytes(w=32, h=32, color=(10, 200, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _box(kind, payload):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def mp4_bytes(duration_s=2.0, w=1920, h=1080, created_unix=FIXED_MTIME, apple_date=None, pad=0, moov_at_end=False):
    """Real box layout: ftyp, moov(mvhd v0, audio trak tkhd 0x0, video trak tkhd WxH [, meta with Apple
    creationdate]) and mdat. moov_at_end puts moov after mdat (as iPhone MOVs do), and mdat then contains a
    decoy 'mvhd' byte sequence, like real compressed data can."""
    timescale = 600
    mvhd = _box(b"mvhd", bytes(4) + (created_unix + QT_EPOCH_OFFSET).to_bytes(4, "big") + bytes(4)
                + timescale.to_bytes(4, "big") + int(duration_s * timescale).to_bytes(4, "big") + bytes(80))

    def trak(tw, th):
        tkhd = _box(b"tkhd", bytes(4) + bytes(20) + bytes(8) + bytes(8) + bytes(36)
                    + (tw << 16).to_bytes(4, "big") + (th << 16).to_bytes(4, "big"))
        return _box(b"trak", tkhd)

    meta = b""
    if apple_date:  # e.g. "2023-07-04T10:00:00-0400"
        meta = _box(b"meta", b"keys....com.apple.quicktime.creationdate....data" + apple_date.encode())
    moov = _box(b"moov", mvhd + trak(0, 0) + trak(w, h) + meta)
    decoy = b"mvhd" + bytes(8) + (600).to_bytes(4, "big") + (7174).to_bytes(4, "big") if moov_at_end else b""
    mdat = _box(b"mdat", decoy + bytes(pad))
    ftyp = _box(b"ftyp", b"mp42" + bytes(4))
    return ftyp + (mdat + moov if moov_at_end else moov + mdat)


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
    """Write a deterministic .tgz, or .zip if the path ends in .zip (Google's default export format).
    entries: list of (relative path under 'Takeout/Google Photos/', bytes)."""
    if str(path).endswith(".zip"):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, data in entries:
                info = zipfile.ZipInfo(f"Takeout/Google Photos/{rel}", date_time=(2023, 11, 14, 22, 13, 20))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)
        return path
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
