"""Pure consolidation logic: match destination items to source items, group exact duplicates,
and decide each image's destination album. No I/O here, so it can be unit tested.
"""

import datetime
import os
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


# ------------------------------------------------------------------ matching

class LegacyIndex:
    """Lookups over legacy Google items: by MD5, by filename, and by HEIC-stem.jpg
    (the destination may store NAME.HEIC as NAME.JPG)."""

    def __init__(self, items, md5_rows):
        self.items = {it["google_id"]: it for it in items}
        self.by_md5 = defaultdict(list)
        for row in md5_rows:
            if row["google_id"] in self.items:
                self.by_md5[row["md5"]].append(row["google_id"])
        self.by_name = defaultdict(list)
        self.by_heic_jpg = defaultdict(list)
        for it in items:
            name = (it["filename"] or "").lower()
            self.by_name[name].append(it["google_id"])
            stem, ext = os.path.splitext(name)
            if ext in (".heic", ".heif"):
                self.by_heic_jpg[stem + ".jpg"].append(it["google_id"])


def match_image(img, index, tz_name):
    """Tie one destination item to a source (legacy Google) item.

    Returns dict(google_id, method, confidence, capture_ts_utc, capture_local, candidates, notes).
    Confidence: high = byte-identical (MD5); medium = unique filename with matching dimensions;
    low = filename match with unknown/mismatched dims, or several candidates in the same month.
    """
    result = {"google_id": None, "method": "none", "confidence": "none", "capture_ts_utc": None,
              "capture_local": None, "candidates": 0, "notes": None}

    def finish(gid, method, confidence, candidates, notes=None):
        ts = index.items[gid]["creation_ts"] if gid else None
        result.update(google_id=gid, method=method, confidence=confidence, capture_ts_utc=ts,
                      capture_local=to_local(ts, tz_name), candidates=candidates, notes=notes)
        return result

    if not img["is_video"] and img.get("md5") in index.by_md5:
        gids = sorted(index.by_md5[img["md5"]], key=lambda g: index.items[g]["creation_ts"] or "")
        return finish(gids[0], "md5" if len(gids) == 1 else "md5_multi", "high", len(gids))

    # Candidates: same filename, plus (for NAME.JPG) Google items named NAME.HEIC/.HEIF, since
    # the destination may rename converted HEICs. Both kinds are considered together: an identically named
    # JPEG is often a different photo than the HEIC this file was converted from.
    name = (img.get("filename") or "").lower()
    cands = list(index.by_name.get(name, []))
    if name.endswith(".jpg"):
        cands += [g for g in index.by_heic_jpg.get(name, []) if g not in cands]
    if not cands:
        return result

    def method_for(gid):
        return "filename" if (index.items[gid]["filename"] or "").lower() == name else "heic_basename"

    def dm(g):
        return dims_match(img.get("width"), img.get("height"), index.items[g]["width"], index.items[g]["height"])

    with_dims = [g for g in cands if dm(g)]
    unknown_dims = [g for g in cands if dm(g) is None]
    if with_dims:
        pool, dims_note, conf = with_dims, "dims_match", "medium"
    elif img["is_video"]:
        # re-encoding may change a video's dimensions, so they can't rule a candidate out
        pool, dims_note, conf = cands, ("dims_unknown" if unknown_dims else "dims_mismatch"), "low"
    elif unknown_dims:
        pool, dims_note, conf = unknown_dims, "dims_unknown", "low"
    else:
        result.update(method="dims_mismatch", candidates=len(cands),
                      notes=f"{len(cands)} same-name candidates, none with matching dimensions")
        return result

    if len(pool) == 1:
        return finish(pool[0], method_for(pool[0]), conf, len(cands), dims_note)

    prefix = method_for(pool[0]) if len({method_for(g) for g in pool}) == 1 else "name"
    months = {(to_local(index.items[g]["creation_ts"], tz_name) or "")[:7] for g in pool}
    if len(months) == 1 and "" not in months:
        first = min(pool, key=lambda g: index.items[g]["creation_ts"] or "")
        return finish(first, prefix + "_same_month", "low", len(cands), f"{dims_note}; {len(pool)} candidates")

    result.update(method=prefix + "_ambiguous", candidates=len(cands), notes=f"{dims_note}; {len(pool)} candidates")
    return result


# ------------------------------------------------------------------ grouping

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
