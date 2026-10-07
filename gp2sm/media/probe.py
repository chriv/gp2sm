"""One entry point for "what is this file?": dimensions, duration and the file's own capture time."""

import datetime

from gp2sm.media.mp4 import clip_creation_ts, mp4_dims, mp4_duration
from gp2sm.media.still import still_info

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".3gp")
STILL_EXTS = (".jpg", ".jpeg", ".heic", ".heif", ".hif", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".dng")


def is_media(ext):
    return ext.lower() in VIDEO_EXTS + STILL_EXTS


def probe(data, ext):
    """{'width', 'height', 'duration_s', 'own_time'}; values are None when unknown. Raises on unreadable stills.

    own_time is ISO 8601: with an offset when the file records one (iPhone EXIF, Apple clip dates), else naive
    local time. Video data may be a `mp4.Partial` (head + tail of a large file); the boxes needed live there.
    """
    ext = ext.lower()
    out = {"width": None, "height": None, "duration_s": None, "own_time": None}
    if ext in VIDEO_EXTS:
        dims = mp4_dims(data)
        if dims:
            out["width"], out["height"] = dims
        out["duration_s"] = mp4_duration(data)
        ts = clip_creation_ts(data)
        if ts:
            out["own_time"] = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    elif ext in STILL_EXTS:
        out.update(still_info(data))
    return out
