"""Album naming (pure): find the date in an album's name, and propose a consistent, date-sortable name.

`parse(name)` returns the date it finds (day, month or year precision), the rest of the name as the subject,
and how sure it is:
  high    an unambiguous full date (2004-03-17, 17 March 2004, March 2004, 2004-03)
  medium  a year alone, a US-style date whose day and month could be swapped, a two-digit year ('19)
  none    no date (numbers that aren't dates: "Route 66", "Part 2", "(2)", "IMG_1234", "Apartment 1204")
`propose(name, ...)` turns that into a new name from templates (default "{yyyy}-{mm} {subject}") and says
whether the change is confident enough to apply automatically. Albums whose names carry no date can still
be dated from their photos (`date_from_photos`): the median capture date when the photos are close
together, never when they span a long time.

Known limit: a name with a full, unambiguous date is trusted as it is. Its photos aren't sampled, so a typo in
the name ("Snow 1-12-2016" for photos taken on 2016-01-21) is carried into the new name. Only names with no
date, a year alone, or a range of years are compared with their photos.
"""

import datetime
import re
import statistics
from dataclasses import dataclass
from typing import Optional

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
MIN_YEAR, CONFIDENCE = 1900, {"high": 2, "medium": 1, "none": 0}

SEP = r"[-./_ ]"
# Each pattern: (name, regex, precision, confidence). Groups: y (4 digits), yy (2 digits), m, mon, d.
PATTERNS = [
    ("y_range", r"(?<!\d)(?P<y>(?:19|20)\d\d)\s*[-/–]\s*(?P<y2>(?:19|20)\d\d)(?!\d)", "range", "none"),
    ("ymd", rf"(?<!\d)(?P<y>\d{{4}}){SEP}(?P<m>\d{{1,2}}){SEP}(?P<d>\d{{1,2}})(?!\d)", "day", "high"),
    ("ymd_compact", r"(?<!\d)(?P<y>(?:19|20)\d\d)(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)", "day", "high"),
    ("d_mon_y", rf"(?<![a-z\d])(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{MONTH_RX})\.?,?\s+(?P<y>\d{{4}})(?!\d)", "day", "high"),
    ("mon_d_y", rf"(?<![a-z])(?P<mon>{MONTH_RX})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})(?!\d)", "day", "high"),
    ("mon_y", rf"(?<![a-z])(?P<mon>{MONTH_RX})\.?,?{SEP}*(?P<y>\d{{4}})(?!\d)", "month", "high"),
    ("ym", rf"(?<!\d)(?P<y>\d{{4}}){SEP}(?P<m>\d{{1,2}})(?![\d])(?!{SEP}\d)", "month", "high"),
    ("mdy", rf"(?<!\d)(?P<m>\d{{1,2}}){SEP}(?P<d>\d{{1,2}}){SEP}(?P<y>\d{{4}})(?!\d)", "day", "us"),
    ("my", rf"(?<!\d)(?P<m>\d{{1,2}}){SEP}(?P<y>\d{{4}})(?!\d)", "month", "high"),
    ("mon_yy", rf"(?<![a-z])(?P<mon>{MONTH_RX})\.?\s*'(?P<yy>\d\d)(?!\d)", "month", "medium"),
    ("y", r"(?<![\d#])(?P<y>(?:19|20)\d\d)(?![\d])", "year", "medium"),
    ("yy", r"(?<![\w])'(?P<yy>\d\d)(?!\d)", "year", "medium"),
]
DANGLING = re.compile(r"\s+(?:taken|on|of|from|in|at|dated|circa|ca\.?)$", re.I)
_COMPILED = [(n, re.compile(rx, re.I), p, c) for n, rx, p, c in PATTERNS]


@dataclass
class Parsed:
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    precision: str = "none"          # day | month | year | range (spans years) | none
    subject: str = ""
    confidence: str = "none"         # high | medium | none
    pattern: Optional[str] = None
    note: str = ""
    year_end: Optional[int] = None   # last year of a range ("2019/2020")

    def iso(self):
        if self.precision == "none":
            return None
        parts = [f"{self.year:04d}"] + ([f"{self.month:02d}"] if self.month else []) + ([f"{self.day:02d}"] if self.day else [])
        return "-".join(parts)


def _valid(y, m=None, d=None, today=None):
    today = today or datetime.date.today()
    if y < MIN_YEAR or y > today.year + 1:
        return False
    if m is not None and not 1 <= m <= 12:
        return False
    if d is not None:
        try:
            datetime.date(y, m, d)
        except ValueError:
            return False
    return True


def _year2(yy, today):
    y = 2000 + yy
    return y if y <= today.year + 1 else 1900 + yy


def clean_subject(text):
    """Tidy what's left once the date is removed: separators at the edges, doubled spaces, empty brackets."""
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" -_.,:;/|–—")
    # words that only made sense next to the date ("Portraits taken on August 3, 2005" -> "Portraits")
    while True:
        trimmed = DANGLING.sub("", text).strip(" -_.,:;/|–—")
        if trimmed == text:
            return text
        text = trimmed


def parse(name, today=None):
    """Find a date in an album name. See the module docstring for the confidence levels."""
    today = today or datetime.date.today()
    for pname, rx, precision, conf in _COMPILED:
        for m in rx.finditer(name or ""):
            g = m.groupdict()
            if pname == "y_range":   # spans years: not one event's date, so the name is left as it is
                return Parsed(int(g["y"]), precision="range", subject=clean_subject(name), pattern=pname,
                              note=f"the name spans {g['y']}-{g['y2']}", year_end=int(g["y2"]))
            y = int(g["y"]) if g.get("y") else _year2(int(g["yy"]), today) if g.get("yy") else None
            mo = MONTHS[g["mon"].lower().rstrip(".")] if g.get("mon") else int(g["m"]) if g.get("m") else None
            d = int(g["d"]) if g.get("d") else None
            note, confidence = "", conf
            if conf == "us":
                if d is not None and d <= 12 and mo != d:
                    confidence, note = "medium", "day and month could be swapped (read as US month/day)"
                elif mo is not None and mo > 12 and d is not None and d <= 12:
                    mo, d, confidence, note = d, mo, "high", "day/month order"
                else:
                    confidence = "high"
            if y is None or not _valid(y, mo, d, today):
                continue
            subject = clean_subject(name[:m.start()] + " " + name[m.end():])
            return Parsed(y, mo, d, precision, subject, confidence, pattern=pname, note=note)
    return Parsed(subject=clean_subject(name or ""))


def date_from_photos(capture_dates, max_spread_days=45, min_photos=5):
    """(Parsed or None, spread_days) from sampled photo dates ('YYYY-MM-DD...' strings): the median date at month
    precision when at least min_photos photos fall within max_spread_days of each other, else None (a wide album
    isn't one date, and a handful of photos isn't evidence)."""
    days = sorted(datetime.date.fromisoformat(d[:10]) for d in capture_dates if d and len(d) >= 10)
    if len(days) < min_photos:
        return None, None
    spread = (days[-1] - days[0]).days
    if spread > max_spread_days:
        return None, spread
    mid = days[0] + datetime.timedelta(days=round(statistics.median((d - days[0]).days for d in days)))
    if spread == 0:   # one event on one day: the full date
        return Parsed(mid.year, mid.month, mid.day, "day", confidence="medium", pattern="photos",
                      note=f"all {len(days)} sampled photos were taken that day"), spread
    return Parsed(mid.year, mid.month, None, "month", confidence="medium", pattern="photos",
                  note=f"from {len(days)} photos within {spread} days"), spread


def render(parsed, subject, templates, keep_day=False):
    """New name from templates: dict(month='{yyyy}-{mm} {subject}', year='{yyyy} {subject}',
    day='{yyyy}-{mm}-{dd} {subject}')."""
    if parsed.precision == "day" and keep_day:
        template = templates["day"]
    elif parsed.precision in ("day", "month"):
        template = templates["month"]
    else:
        template = templates["year"]
    text = template.format(yyyy=f"{parsed.year:04d}", mm=f"{parsed.month or 0:02d}", dd=f"{parsed.day or 0:02d}",
                           subject=subject)
    return clean_subject(text) if subject else clean_subject(text.replace("{subject}", ""))


@dataclass
class Proposal:
    old: str
    new: Optional[str]
    date: Optional[str]
    source: str                       # name | photos | none
    confidence: str
    auto: bool                        # confident enough to apply without review
    note: str = ""


def spread_days(dates):
    days = sorted(datetime.date.fromisoformat(d[:10]) for d in dates or () if d and len(d) >= 10)
    return (days[-1] - days[0]).days if days else None


def propose(name, templates, keep_day=True, min_confidence="high", photo_dates=None, max_spread_days=45,
            today=None, min_photos=5, upload_dates=None):
    """A rename proposal for one album name. photo_dates (optional) date an album whose name has no date, or add
    the month (or day) to a name with only a year when the photos fall in that year; unless upload_dates show the
    album kept growing over a long time (an uploader's album, not one event)."""
    p = parse(name, today)
    source = "name"
    if p.precision == "year" and photo_dates and not (spread_days(upload_dates) or 0) > max_spread_days:
        p2, _ = date_from_photos(photo_dates, max_spread_days, min_photos)
        if p2 is not None and p2.year == p.year:   # name and photos agree: the photos add the month (or day)
            p = Parsed(p.year, p2.month, p2.day, p2.precision, p.subject, "high", pattern="name+photos",
                       note=f"year from the name; {p2.note}")
            source = "name+photos"
    if p.precision == "range":
        # often a school or season year; one tightly clustered event inside it gets the photos' date, range kept
        if photo_dates and not (spread_days(upload_dates) or 0) > max_spread_days:
            p2, _ = date_from_photos(photo_dates, max_spread_days, min_photos)
            if p2 is not None and p.year <= p2.year <= p.year_end:
                p = Parsed(p2.year, p2.month, p2.day, p2.precision, p.subject, "high", pattern="range+photos",
                           note=f"{p.note} (kept in the name); {p2.note}")
                source = "range+photos"
        if p.precision == "range":
            return Proposal(name, None, None, "none", "none", False, f"{p.note}; left as it is")
    if p.precision == "none":
        uploads = spread_days(upload_dates)
        if photo_dates and uploads is not None and uploads > max_spread_days:
            return Proposal(name, None, None, "none", "none", False,
                            f"no date in the name; its photos were uploaded over {uploads} days")
        if photo_dates:
            p2, spread = date_from_photos(photo_dates, max_spread_days, min_photos)
            if p2 is None:
                note = "no date in the name" + (f"; its photos span {spread} days" if spread is not None
                                                else f"; fewer than {min_photos} dated photos")
                return Proposal(name, None, None, "none", "none", False, note)
            p2.subject = p.subject
            p, source = p2, "photos"
        else:
            return Proposal(name, None, None, "none", "none", False, "no date in the name")
    new = render(p, p.subject, templates, keep_day)
    if new == name:
        return Proposal(name, None, p.iso(), source, p.confidence, False, "already follows the convention")
    auto = CONFIDENCE[p.confidence] >= CONFIDENCE[min_confidence]
    return Proposal(name, new, p.iso(), source, p.confidence, auto, p.note)
