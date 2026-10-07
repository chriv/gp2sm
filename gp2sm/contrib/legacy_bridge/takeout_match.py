"""Link a Takeout index to the legacy transfer database and the consolidation state (legacy bridge).

Builds the index DB's `items` table: Takeout items (gp2sm.takeout.items) matched to legacy Google items by
(filename, capture time) and, through the consolidation DB, to the SmugMug copy placed there.

Usage: gp2sm takeout-match [--index data/takeout_index.db] [--state data/consolidation.db]
"""

import argparse
import logging
import os
import sqlite3
import sys
from collections import defaultdict

from gp2sm.project import context
from gp2sm.takeout.items import MOTION_EXTS, build_items, epoch_of

log = logging.getLogger("gp2sm.contrib.legacy_bridge.takeout_match")

SCHEMA = """
DROP TABLE IF EXISTS items;
CREATE TABLE items(
  item_id INTEGER PRIMARY KEY, archive TEXT, path TEXT, folder TEXT, filename TEXT, ext TEXT,
  size INT, md5 TEXT, is_video INT,
  sidecar_path TEXT, title TEXT, taken_ts INT, created_ts INT, url TEXT, lat REAL, lon REAL,
  motion_path TEXT, motion_size INT, motion_md5 TEXT,
  google_id TEXT, legacy_match TEXT,
  smug_image_key TEXT, smug_album TEXT, smug_match_method TEXT, smug_confidence TEXT,
  status TEXT);
DROP TABLE IF EXISTS orphans;
CREATE TABLE orphans(archive TEXT, path TEXT, kind TEXT, size INT);
"""



def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm takeout-match", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    context.add_args(p, paths=("index", "state"))
    args = p.parse_args(argv)
    context.resolve(args)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)

    idx = sqlite3.connect(args.index)
    idx.row_factory = sqlite3.Row
    items, orphans = build_items(idx)

    st = sqlite3.connect(f"file:{os.path.abspath(args.state)}?mode=ro", uri=True)
    st.row_factory = sqlite3.Row
    legacy = defaultdict(list)
    for r in st.execute("SELECT google_id, filename, creation_ts FROM legacy_items"):
        legacy[(r["filename"] or "").lower()].append((epoch_of(r["creation_ts"]), r["google_id"]))
    smug = {r["google_id"]: dict(r) for r in st.execute(
        "SELECT m.google_id, m.image_key, m.method, m.confidence, p.target_name FROM matches m "
        "JOIN dup_groups d USING(image_key) JOIN plan p USING(image_key) "
        "WHERE d.is_keeper=1 AND p.status='done' AND m.google_id IS NOT NULL "
        "ORDER BY CASE m.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END")}
    # keep the best-confidence keeper per google_id (ORDER BY above; first wins)
    smug_best = {}
    for gid, row in smug.items():
        smug_best.setdefault(gid, row)

    idx.executescript(SCHEMA)
    rows = []
    for it in items:
        js, m, s, mo = it["js"], it["media"], it["sidecar"], it.get("motion")
        title = js.get("title")
        taken = int(js.get("photoTakenTime", {}).get("timestamp") or 0) or None
        created = int(js.get("creationTime", {}).get("timestamp") or 0) or None
        geo = js.get("geoData") or {}
        gid = legacy_match = None
        cands = legacy.get((title or "").lower(), [])
        exact = [g for ts, g in cands if ts is not None and taken is not None and abs(ts - taken) <= 1]
        if len(exact) == 1:
            gid, legacy_match = exact[0], "title+taken"
        elif len(exact) > 1:
            legacy_match = f"ambiguous({len(exact)})"
        elif len(cands) == 1:
            gid, legacy_match = cands[0][1], "title_only"
        else:
            legacy_match = "none" if not cands else f"title_multi({len(cands)})"
        sm = smug_best.get(gid) if gid else None
        if m is None:
            status = "sidecar_without_media"
        elif sm:
            status = "on_smugmug"
        else:
            status = "missing_on_smugmug"
        ext = m["ext"] if m else None
        rows.append((m["archive"] if m else s["archive"], m["path"] if m else None, s["folder"],
                     m["basename"] if m else None, ext, m["size"] if m else None, m["md5"] if m else None,
                     int(ext in MOTION_EXTS) if m else None, s["path"], title, taken, created, js.get("url"),
                     geo.get("latitude") or None, geo.get("longitude") or None,
                     mo["path"] if mo else None, mo["size"] if mo else None, mo["md5"] if mo else None,
                     gid, legacy_match, sm["image_key"] if sm else None, sm["target_name"] if sm else None,
                     sm["method"] if sm else None, sm["confidence"] if sm else None, status))
    idx.executemany("INSERT INTO items(archive, path, folder, filename, ext, size, md5, is_video, sidecar_path, title,"
                    " taken_ts, created_ts, url, lat, lon, motion_path, motion_size, motion_md5, google_id,"
                    " legacy_match, smug_image_key, smug_album, smug_match_method, smug_confidence, status)"
                    " VALUES(" + ",".join("?" * 25) + ")", rows)
    for o in orphans:
        kind = "motion_without_still" if o["ext"] in MOTION_EXTS else "media_without_sidecar"
        idx.execute("INSERT INTO orphans VALUES(?,?,?,?)", (o["archive"], o["path"], kind, o["size"]))
    idx.commit()

    def show(title, sql):
        print(f"\n== {title} ==")
        for r in idx.execute(sql):
            print("  " + "  ".join(str(x) for x in r))

    show("Items by status", "SELECT status, COUNT(*), SUM(motion_path IS NOT NULL) live FROM items GROUP BY 1")
    show("Legacy match", "SELECT legacy_match, COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC")
    show("Missing on SmugMug, by type", "SELECT ext, COUNT(*), round(SUM(size)/1e9,2) gb FROM items "
                                        "WHERE status='missing_on_smugmug' GROUP BY 1 ORDER BY 2 DESC")
    show("Orphans", "SELECT kind, COUNT(*), round(SUM(size)/1e9,2) gb FROM orphans GROUP BY 1")
    show("Distinct google_ids matched / legacy total",
         "SELECT COUNT(DISTINCT google_id) FROM items WHERE google_id IS NOT NULL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
