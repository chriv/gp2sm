"""Dates from the old v1/v2 tool's transfer databases (legacy bridge).

`gp2sm consolidate import-legacy` copies the Google item lists (ids, filenames, capture times, MD5s) into
the state DB, and `match` ties each destination item to one of them (by MD5, then filename + dimensions).
`gp2sm organize` can then use those dates as the `legacy` date source (`plugin`).
"""

import json
import logging
import os
import sqlite3
from collections import defaultdict

from gp2sm.organize import planning
from gp2sm.organize.planning import dims_match, to_local

log = logging.getLogger("gp2sm.contrib.legacy_bridge.legacy_dates")


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


# ------------------------------------------------------------ import-legacy

def cmd_import_legacy(st, cfg, client, args):
    stats = {}
    for path in cfg["legacy_dbs"]:
        src = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "media_items" not in tables:
            log.warning("%s has no media_items table; skipped", path)
            continue
        n = n_md5 = 0
        with st.db:
            for r in src.execute("SELECT * FROM media_items"):
                md = json.loads(r["media_metadata_json"] or "{}")
                st.db.execute(
                    "INSERT INTO legacy_items(google_id, filename, mime_type, creation_ts, width, height, product_url,"
                    " legacy_status, source_db) VALUES(?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(google_id) DO UPDATE SET"
                    " creation_ts=COALESCE(legacy_items.creation_ts, excluded.creation_ts),"
                    " width=COALESCE(legacy_items.width, excluded.width),"
                    " height=COALESCE(legacy_items.height, excluded.height)",
                    (r["google_id"], r["filename"], r["mime_type"],
                     r["creation_timestamp"] or md.get("creationTime"),
                     int(md["width"]) if md.get("width") else None, int(md["height"]) if md.get("height") else None,
                     r["product_url"], r["status"], os.path.basename(path)))
                n += 1
                if r["md5_hash"]:
                    st.db.execute("INSERT OR IGNORE INTO legacy_md5 VALUES(?,?,?)",
                                  (r["google_id"], r["md5_hash"], os.path.basename(path)))
                    n_md5 += 1
            st.event("import_legacy", commit=False, path=os.path.basename(path), items=n, md5=n_md5)
        stats[os.path.basename(path)] = {"items": n, "md5": n_md5}
        log.info("imported %s: %d items, %d md5", path, n, n_md5)
    stats["distinct_items"] = st.one("SELECT COUNT(*) FROM legacy_items")
    stats["distinct_md5"] = st.one("SELECT COUNT(DISTINCT md5) FROM legacy_md5")
    return stats


# -------------------------------------------------------------------- match

def load_images(st):
    return [dict(r) for r in st.q("SELECT image_key AS item_id, filename, is_video, archived_md5 AS md5, width, height, "
                                  "uploaded FROM images")]


def cmd_match(st, cfg, client, args):
    index = LegacyIndex([dict(r) for r in st.q("SELECT * FROM legacy_items")],
                                 [dict(r) for r in st.q("SELECT google_id, md5 FROM legacy_md5")])
    images = load_images(st)
    matches = {img["item_id"]: match_image(img, index, cfg["timezone"]) for img in images}
    groups = planning.group_duplicates(images, matches)
    with st.db:
        st.db.execute("DELETE FROM matches")
        st.db.executemany("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?)",
                          [(k, m["google_id"], m["method"], m["confidence"], m["capture_ts_utc"],
                            m["capture_local"], m["candidates"], m["notes"]) for k, m in matches.items()])
        st.db.execute("DELETE FROM dup_groups")
        st.db.executemany("INSERT INTO dup_groups VALUES(?,?,?,?,?)",
                          [(k, g["group_key"], g["group_size"], g["keeper_item_id"], g["is_keeper"])
                           for k, g in groups.items()])
    summary = {r[0]: r[1] for r in st.q("SELECT method, COUNT(*) FROM matches GROUP BY method")}
    summary["keepers"] = st.one("SELECT COUNT(*) FROM dup_groups WHERE is_keeper=1")
    summary["duplicates"] = st.one("SELECT COUNT(*) FROM dup_groups WHERE is_keeper=0")
    st.event("match", **summary)
    return summary



def plugin(st):
    """The `legacy` date source for organize: capture dates linked by `match`."""
    legacy = dict(st.q("SELECT image_key, capture_local FROM matches WHERE capture_local IS NOT NULL"))
    return lambda item: legacy.get(item["item_id"])
