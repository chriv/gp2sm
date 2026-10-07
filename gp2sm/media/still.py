"""Still-image header facts (dimensions, camera capture time) read with Pillow, without decoding pixels."""

import io

from PIL import Image

from gp2sm.media import convert  # noqa: F401  (registers the HEIC/HEIF opener)

EXIF_IFD = 0x8769
DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011


def exif_time(exif):
    """Camera capture time as ISO 8601 ('YYYY-MM-DDTHH:MM:SS', plus '+HH:MM' when the offset is recorded), or None."""
    ifd = exif.get_ifd(EXIF_IFD)
    raw = ifd.get(DATETIME_ORIGINAL)
    if not raw or not isinstance(raw, str) or len(raw) < 19:
        return None
    date, _, clock = raw.strip().partition(" ")
    parts = date.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts) or parts == ["0000", "00", "00"]:
        return None
    text = f"{parts[0]}-{parts[1]}-{parts[2]}T{clock[:8]}"
    offset = ifd.get(OFFSET_TIME_ORIGINAL)
    if isinstance(offset, str) and len(offset.strip()) == 6 and offset.strip()[0] in "+-":
        text += offset.strip()
    return text


def still_info(data):
    """{'width', 'height', 'own_time'} for image bytes in any format Pillow (+ pillow-heif) can open."""
    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        return {"width": w, "height": h, "own_time": exif_time(img.getexif())}
