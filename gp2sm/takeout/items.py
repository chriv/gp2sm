"""Google Takeout items from the index: each media file with its metadata sidecar and Live Photo clip.

1. pair each `*.supplemental-metadata*.json` sidecar with its media file (same folder); handles Google's
   "(N)" collision suffix (sidecar `x.heic.supplemental-metadata(3).json` -> media `x(3).heic`) and
   truncated sidecar names
2. attach Live Photo motion files (MP4/MOV sharing a stem with a still) to their still
Everything else is an orphan (media without a sidecar, or a clip without a still), never silently dropped.
"""

import datetime
import json
import os
import re
from collections import defaultdict

STILL_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".dng", ""}
MOTION_EXTS = {".mp4", ".mov"}
SIDECAR_RE = re.compile(r"^(?P<stem>.*?)\.(?:s(?:u(?:p(?:p(?:l(?:e(?:m(?:e(?:n(?:t(?:a(?:l(?:-(?:m(?:e(?:t(?:a(?:d(?:a(?:t(?:a)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?(?P<n>\(\d+\))?\.json$", re.I)

def sidecar_media_name(sidecar_basename, title):
    """Expected media basename for a sidecar, or None if the sidecar name isn't recognizable."""
    m = SIDECAR_RE.match(sidecar_basename)
    if not m:
        return None, None
    n = m.group("n") or ""
    stem = m.group("stem")  # media name (possibly truncated), e.g. "IMG_1.HEIC"
    if n:
        base, ext = os.path.splitext(title or stem)
        return f"{base}{n}{ext}", stem
    return stem, stem


def epoch_of(ts):
    if not ts:
        return None
    return int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def build_items(idx):
    members = [dict(r) for r in idx.execute("SELECT * FROM members")]
    by_folder = defaultdict(dict)
    for m in members:
        if m["ext"] != ".json":
            by_folder[m["folder"]][m["basename"].lower()] = m
    used = set()
    items = []

    # 1. sidecars -> media
    for s in members:
        if s["ext"] != ".json" or "supplemental-metadata" not in s["basename"].lower() and \
                not SIDECAR_RE.match(s["basename"]) or s["basename"].lower() == "metadata.json":
            continue
        try:
            js = json.loads(s["json"] or "{}")
        except ValueError:
            js = {}
        if "photoTakenTime" not in js and "title" not in js:
            continue
        expected, stem = sidecar_media_name(s["basename"], js.get("title"))
        folder = by_folder.get(s["folder"], {})
        media = folder.get((expected or "").lower())
        if media is None and stem:
            # truncated sidecar names: unique unused media in folder starting with the stem
            cands = [m for k, m in folder.items() if k.startswith(stem.lower()) and (m["archive"], m["path"]) not in used]
            if len(cands) == 1:
                media = cands[0]
        if media is None or (media["archive"], media["path"]) in used:
            items.append({"sidecar": s, "js": js, "media": None})
            continue
        used.add((media["archive"], media["path"]))
        items.append({"sidecar": s, "js": js, "media": media})

    # 2. motion files sharing a stem with a paired still
    still_by_stem = {}
    for it in items:
        m = it["media"]
        if m and m["ext"] in STILL_EXTS - {".png", ".gif"}:
            still_by_stem[(m["folder"], os.path.splitext(m["basename"])[0].lower())] = it
    orphans = []
    for m in members:
        if m["ext"] == ".json" or (m["archive"], m["path"]) in used:
            continue
        key = (m["folder"], os.path.splitext(m["basename"])[0].lower())
        if m["ext"] in MOTION_EXTS and key in still_by_stem and "motion" not in still_by_stem[key]:
            still_by_stem[key]["motion"] = m
            used.add((m["archive"], m["path"]))
        else:
            orphans.append(m)
    return items, orphans


