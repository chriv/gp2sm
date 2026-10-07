"""Image conversion and rendering, cross-platform.

Default backend: Pillow + pillow-heif (libheif), on Windows, Linux and macOS. Optional backend "sips" (macOS only).
HEIC orientation: libheif applies the container's rotation/mirroring when decoding, so the JPEG's EXIF
Orientation is reset to 1. Otherwise viewers would rotate the image a second time.
"""

import io
import os
import subprocess
import tempfile

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()   # registered once; HEIC/HEIF then opens like any other format

HEIF_EXTS = (".heic", ".heif", ".hif")


def default_backend():
    return "pillow"


def to_jpeg(data, src_ext, dest, quality=92, backend=None):
    """Convert image bytes to a JPEG at `dest`, keeping EXIF (dates, offset, camera, GPS) and the ICC profile."""
    backend = backend or default_backend()
    if backend == "sips":
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src" + src_ext)
            with open(src, "wb") as f:
                f.write(data)
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality), src, "--out", dest],
                           check=True, capture_output=True, timeout=300)
        return
    img = Image.open(io.BytesIO(data))
    raw_exif = img.info.get("exif") or b""
    if raw_exif and src_ext.lower() in HEIF_EXTS:
        raw_exif = _reset_orientation(raw_exif)  # pixels already oriented by libheif
    params = {"quality": quality}
    if raw_exif:
        params["exif"] = raw_exif  # original bytes: re-encoding through Pillow drops tags (e.g. GPSTimeStamp)
    if img.info.get("icc_profile"):
        params["icc_profile"] = img.info["icc_profile"]
    img.convert("RGB").save(dest, "JPEG", **params)


def _reset_orientation(raw_exif):
    """Set Orientation=1 in raw EXIF bytes, leaving every other tag exactly as it was."""
    import piexif
    body = raw_exif[6:] if raw_exif.startswith(b"Exif\x00\x00") else raw_exif
    try:
        ex = piexif.load(body)
    except Exception:
        return raw_exif
    if ex["0th"].get(piexif.ImageIFD.Orientation, 1) == 1:
        return raw_exif
    ex["0th"][piexif.ImageIFD.Orientation] = 1
    ex.pop("thumbnail", None)
    ex["1st"] = {}
    return piexif.dump(ex)


def render_small(data, max_side=400):
    """A small, display-oriented rendering (for perceptual hashing)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    img.thumbnail((max_side, max_side))
    return img.convert("RGB")
