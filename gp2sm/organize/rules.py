"""Organize rules (pure): when was each item taken, which group does it belong to, and which album is that.

Dates come from an ordered chain; the first source that gives a plausible date wins and is recorded:
  camera    the capture time the destination read from the file (EXIF DateTimeOriginal / video creation time)
  filename  a date in the file name, from common camera, phone and app patterns (FILENAME_PATTERNS)
  album     a date in the source album's name (e.g. "2004-03-17 New puppy"), with the same patterns
  upload    when the item was uploaded (a last resort; off unless listed)
Other sources can be added by plugins (date_for's `plugins`).

Groups come from ordered rules; the first rule whose conditions all match names the group. Conditions are
case-insensitive globs on the source album, camera make, camera model and file name.
"""

import datetime
import fnmatch
import re
from zoneinfo import ZoneInfo

MIN_YEAR = 1990   # earlier "dates" are almost always camera defaults or garbage

# (name, regex, has_time). Groups: y, m, d and optionally H, M, S.
FILENAME_PATTERNS = [
    ("camera_datetime", r"(?:^|[^0-9])(?:IMG|VID|PXL|MVIMG|PANO|BURST)?_?(?P<y>\d{4})(?P<m>\d\d)(?P<d>\d\d)[_-](?P<H>\d\d)(?P<M>\d\d)(?P<S>\d\d)", True),
    ("screenshot", r"Screen ?[Ss]hot[ _](?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)(?:(?: at |[ _-])(?P<H>\d{1,2})[.:-](?P<M>\d\d)[.:-](?P<S>\d\d)(?:\s?(?P<ampm>[AP]M))?)?", True),
    ("whatsapp", r"(?:IMG|VID)-(?P<y>\d{4})(?P<m>\d\d)(?P<d>\d\d)-WA\d+", False),
    ("iso_date", r"(?:^|[^0-9])(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)(?:[ T_](?P<H>\d\d)[.:-](?P<M>\d\d)[.:-](?P<S>\d\d))?(?:[^0-9]|$)", True),
]
_COMPILED = [(name, re.compile(rx), has_time) for name, rx, has_time in FILENAME_PATTERNS]


def _plausible(y, m, d, today):
    try:
        dt = datetime.date(int(y), int(m), int(d))
    except ValueError:
        return False
    return MIN_YEAR <= dt.year and dt <= today + datetime.timedelta(days=1)


def date_from_filename(name, today=None):
    """(local ISO date or datetime, pattern name) found in a file name, or (None, None)."""
    today = today or datetime.date.today()
    for pname, rx, _ in _COMPILED:
        m = rx.search(name or "")
        if not m or not _plausible(m["y"], m["m"], m["d"], today):
            continue
        g = m.groupdict()
        if g.get("H") and int(g["H"]) < 24 and int(g["M"]) < 60 and int(g["S"]) < 60:
            hour = int(g["H"])
            if g.get("ampm"):   # 12-hour clock (older macOS screenshot names)
                hour = hour % 12 + (12 if g["ampm"] == "PM" else 0)
            return f"{g['y']}-{g['m']}-{g['d']}T{hour:02d}:{g['M']}:{g['S']}", pname
        return f"{g['y']}-{g['m']}-{g['d']}", pname
    return None, None


def camera_date(capture_time, today=None):
    """The destination's capture time (naive local, e.g. '2023-05-04T10:00:00') if plausible, else None."""
    if not capture_time:
        return None
    text = capture_time.replace(" ", "T")[:19]
    if len(text) < 10 or not _plausible(text[:4], text[5:7], text[8:10], today or datetime.date.today()):
        return None
    return text


def upload_date(uploaded, tz_name):
    """Upload time (ISO with offset) -> local naive ISO, or None."""
    if not uploaded:
        return None
    dt = datetime.datetime.fromisoformat(uploaded.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.isoformat(timespec="seconds")
    return dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None).isoformat(timespec="seconds")


def date_for(item, chain, tz_name, plugins=None, today=None):
    """(capture_local or None, method) for an item dict(filename, capture_time, uploaded, item_id, ...).
    plugins: {name: fn(item) -> capture_local or None} for chain entries beyond the built-ins."""
    for source in chain:
        if source == "camera":
            value = camera_date(item.get("capture_time"), today)
        elif source == "filename":
            value, pattern = date_from_filename(item.get("filename"), today)
            if value:
                return value, f"filename:{pattern}"
            continue
        elif source == "album":
            value, pattern = date_from_filename(item.get("album_name"), today)
            if value:
                return value, f"album:{pattern}"
            continue
        elif source == "upload":
            value = upload_date(item.get("uploaded"), tz_name)
        else:
            fn = (plugins or {}).get(source)
            if fn is None:
                raise ValueError(f"unknown date source {source!r}")
            value = fn(item)
        if value:
            return value, source
    return None, "none"


def _glob(pattern, value):
    return fnmatch.fnmatchcase((value or "").lower(), pattern.lower())


def group_for(item, rules, unassigned):
    """The first rule whose conditions all match names the group; otherwise `unassigned`.
    rules: [dict(name, album=glob, make=glob, model=glob, filename=glob)] (conditions optional).
    item: dict(album, make, model, filename)."""
    for rule in rules:
        conditions = [(k, v) for k, v in rule.items() if k != "name"]
        if all(_glob(v, item.get(k)) for k, v in conditions):
            return rule["name"]
    return unassigned


def album_name(template, capture_local, group):
    """Fill an album template ({yyyy}, {mm}, {group}); the result is tidied of doubled or edge spaces.
    A "/" separates subfolders from the album name ("{yyyy}/{yyyy}-{mm}"); empty segments are dropped."""
    yyyy, mm = (capture_local[:4], capture_local[5:7]) if capture_local else ("", "")
    filled = template.format(yyyy=yyyy, mm=mm, group=group or "")
    return "/".join(seg for seg in (" ".join(part.split()) for part in filled.split("/")) if seg)


def recent(uploaded, days, now=None):
    """True if the item was uploaded within the last `days` days (0 = never skip)."""
    if not days or not uploaded:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    dt = datetime.datetime.fromisoformat(uploaded.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return now - dt < datetime.timedelta(days=days)
