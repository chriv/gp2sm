"""Pure planning: group exact duplicates and decide each item's destination album (consolidation and
organize). No I/O here, so it can be unit tested. Matching to the legacy transfer database lives in
gp2sm.contrib.legacy_bridge.legacy_dates.
"""

import datetime
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, None: 0, "none": 0}


# ----------------------------------------------------------------- utilities

def parse_duration(value):
    """'6.08 s' / '4' / '' -> float seconds or None."""
    if value in (None, ""):
        return None
    m = re.search(r"[\d.]+", str(value))
    return float(m.group()) if m else None


def to_local(ts_utc, tz_name):
    """'2023-05-01T12:00:00Z' -> local ISO string (no offset), or None."""
    if not ts_utc:
        return None
    dt = datetime.datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None).isoformat(timespec="seconds")


def dims_match(w1, h1, w2, h2):
    if not (w1 and h1 and w2 and h2):
        return None  # unknown
    return (w1, h1) == (w2, h2) or (w1, h1) == (h2, w2)


def group_duplicates(images, matches):
    """Group items by content hash (md5) and choose one keeper per group.

    The destination's video re-encoding is deterministic, so identical uploads share a stored hash for
    videos too. Keeper: best match confidence, then earliest upload, then item_id.
    Returns {item_id: dict(group_key, group_size, keeper_item_id, is_keeper)}.
    """
    groups = defaultdict(list)
    for img in images:
        key = img.get("md5") or f"nomd5:{img['item_id']}"
        groups[key].append(img)

    out = {}
    for key, members in groups.items():
        def rank(img):
            m = matches.get(img["item_id"], {})
            return (-CONFIDENCE_RANK.get(m.get("confidence"), 0), img.get("uploaded") or "", img["item_id"])
        keeper = min(members, key=rank)
        for img in members:
            out[img["item_id"]] = {"group_key": key, "group_size": len(members),
                                     "keeper_item_id": keeper["item_id"],
                                     "is_keeper": int(img is keeper)}
    return out


def group_capture(groups, matches):
    """Best-known capture time per group: identical bytes are the same item, so any member's
    match can date the keeper. Returns {group_key: match dict}."""
    best = {}
    for item_id, g in groups.items():
        m = matches.get(item_id)
        if not m or not m.get("capture_local"):
            continue
        cur = best.get(g["group_key"])
        if cur is None or CONFIDENCE_RANK[m["confidence"]] > CONFIDENCE_RANK[cur["confidence"]]:
            best[g["group_key"]] = m
    return best


# ------------------------------------------------------------------ planning

def base_target(is_video, capture_local, cfg):
    if is_video:
        if not capture_local:
            return cfg["undated_video_album"], "video_undated"
        yyyy, mm = capture_local[:4], capture_local[5:7]
        return cfg["video_album_template"].format(yyyy=yyyy, mm=mm), "video"
    if not capture_local:
        return cfg["undated_photo_album"], "photo_undated"
    yyyy, mm = capture_local[:4], capture_local[5:7]
    return cfg["photo_album_template"].format(yyyy=yyyy, mm=mm), "photo"


def part_name(base, n):
    return base if n == 1 else f"{base} - Part {n}"


def plan_actions(images, matches, groups, cfg, existing=None):
    """Decide action + target album for every image.

    Keepers move to a dated (or undated) album; non-keepers move to the duplicates album, which
    is reversible (nothing is deleted). Targets over cfg['album_soft_cap'] split into
    '<name> - Part N' in capture order. Items whose plan status is already 'done' keep their target.
    Returns list of dict(item_id, action, target_name, kind, reason).
    """
    existing = existing or {}
    gcap = group_capture(groups, matches)
    rows = []
    for img in images:
        g = groups[img["item_id"]]
        if g["is_keeper"]:
            m = gcap.get(g["group_key"]) or matches.get(img["item_id"], {})
            base, kind = base_target(img["is_video"], m.get("capture_local"), cfg)
            reason = f"keeper; date via {m.get('method', 'none')} ({m.get('confidence', 'none')})"
            rows.append({"item_id": img["item_id"], "action": "move", "base": base, "kind": kind,
                         "sort": (m.get("capture_local") or "", img["item_id"]), "reason": reason})
        else:
            rows.append({"item_id": img["item_id"], "action": "park_duplicate",
                         "base": cfg["duplicates_album"], "kind": "duplicates",
                         "sort": (g["group_key"], img["item_id"]),
                         "reason": f"duplicate of {g['keeper_item_id']} ({g['group_size']} copies)"})

    cap = cfg["album_soft_cap"]
    by_base = defaultdict(list)
    for r in rows:
        by_base[r["base"]].append(r)
    for base, members in by_base.items():
        members.sort(key=lambda r: r["sort"])
        for i, r in enumerate(members):
            prior = existing.get(r["item_id"])
            if prior and prior["status"] == "done":
                r["target_name"] = prior["target_name"]
            else:
                r["target_name"] = part_name(base, i // cap + 1)
    return [{k: r[k] for k in ("item_id", "action", "target_name", "kind", "reason")} for r in rows]


# ------------------------------------------------------------- organize (rules)

def plan_organize(items, settings, existing=None):
    """Targets for items already dated and grouped by organize.rules (pure).

    items: [dict(item_id, filename, is_video, md5, uploaded, capture_local, method, group)]
    settings: dict(photo, video, undated_photo, undated_video (templates; {yyyy} {mm} {group}),
                   duplicates_album, duplicates ('park' | 'keep'), soft_cap, mode ('move' | 'collect'))
    existing: {item_id: dict(status, target_name)}; items whose move is done keep their album.
    Byte-identical copies: with 'park', one keeper per MD5 (earliest upload) is organized and the others go to
    the duplicates album; any copy's date can date the keeper. Albums over soft_cap continue as '- Part N'.
    mode 'collect' adds items to their album and leaves them in the source (action 'collect'); nothing is
    parked then, since moving items out of a source an uploader app still writes to may make it upload again.
    Returns [dict(item_id, action, target_name, kind, reason)].
    """
    existing = existing or {}
    collect = settings.get("mode") == "collect"
    groups = {}
    if settings["duplicates"] == "park" and not collect:
        for it in items:
            if it.get("md5"):
                groups.setdefault(it["md5"], []).append(it)
    keeper_of, dated_by = {}, {}
    for members in groups.values():
        keeper = min(members, key=lambda it: (it.get("uploaded") or "", it["item_id"]))
        dated = next((m for m in sorted(members, key=lambda m: m is not keeper) if m.get("capture_local")), None)
        for m in members:
            keeper_of[m["item_id"]] = keeper["item_id"]
        if dated:
            dated_by[keeper["item_id"]] = dated

    def fill(template, capture_local, group):
        yyyy, mm = (capture_local[:4], capture_local[5:7]) if capture_local else ("", "")
        return " ".join(template.format(yyyy=yyyy, mm=mm, group=group or "").split())

    rows = []
    for it in items:
        keeper = keeper_of.get(it["item_id"], it["item_id"])
        if keeper != it["item_id"]:
            rows.append({"item_id": it["item_id"], "action": "park_duplicate", "base": settings["duplicates_album"],
                         "kind": "duplicates", "sort": (it["md5"], it["item_id"]),
                         "reason": f"same file as {keeper}"})
            continue
        src = dated_by.get(it["item_id"], it)
        when, method = src.get("capture_local"), src.get("method") or "none"
        if src is not it:
            method = f"{method} (from an identical copy)"
        video = bool(it.get("is_video"))
        if when:
            base, kind = fill(settings["video" if video else "photo"], when, it.get("group")), "video" if video else "photo"
        else:
            base = fill(settings["undated_video" if video else "undated_photo"], None, it.get("group"))
            kind = "video_undated" if video else "photo_undated"
        group = f", group {it['group']}" if it.get("group") else ""
        rows.append({"item_id": it["item_id"], "action": "collect" if collect else "move", "base": base, "kind": kind,
                     "sort": (when or "", it["item_id"]), "reason": f"date via {method}{group}"})

    by_base = defaultdict(list)
    for r in rows:
        by_base[r["base"]].append(r)
    for base, members in by_base.items():
        members.sort(key=lambda r: r["sort"])
        for i, r in enumerate(members):
            prior = existing.get(r["item_id"])
            if prior and prior["status"] == "done":
                r["target_name"] = prior["target_name"]
            else:
                r["target_name"] = part_name(base, i // settings["soft_cap"] + 1)
    return [{k: r[k] for k in ("item_id", "action", "target_name", "kind", "reason")} for r in rows]
