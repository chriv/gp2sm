"""Perceptual (content) matching of unlinked Takeout stills to unlinked SmugMug copies.

Legacy uploads came from Google's re-processed downloads, so their bytes (and often names) differ from
the Takeout originals. Matching uses a 64-bit difference hash (dHash) of small renderings:
calibration on known pairs gave distances 0-4 for true matches and >=19 for different photos.

Candidates share a base name (e.g. `IMG_2155(1).HEIC` ~ `IMG_2155.JPG`, `lp_image(N).heic` ~ `lp_image.JPG`).
Decision per Takeout item: 'match' if best <= MATCH_MAX and next-best >= MARGIN_MIN (each SmugMug
image is used at most once, closest pairs first); 'review' if best <= REVIEW_MAX; otherwise 'none'.

  gp2sm content-match            # compute (resumable; hashes are cached)
  gp2sm content-match --apply    # write matches into the takeout + consolidation DBs
"""

import argparse
import concurrent.futures
import io
import logging
import os
import re
import sqlite3
import sys

from PIL import Image

from gp2sm.media.convert import render_small
from gp2sm.media.phash import dhash, hamming, rotation_hashes  # noqa: F401  (re-exported for date_undated)
from gp2sm.organize import planning
from gp2sm.project import context
from gp2sm.state import State, now
from gp2sm.takeout.archive import read_members

log = logging.getLogger("gp2sm.organize.content_match")

MATCH_MAX = 6
MARGIN_MIN = 12
REVIEW_MAX = 11
STILL_EXTS = (".heic", ".heif", ".jpg", ".jpeg", ".png")

SCHEMA = """
CREATE TABLE IF NOT EXISTS phash_takeout(item_id INTEGER PRIMARY KEY, h0 TEXT, h1 TEXT, h2 TEXT, h3 TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS phash_smug(image_key TEXT PRIMARY KEY, h TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS content_matches(
  item_id INTEGER PRIMARY KEY, decision TEXT, image_key TEXT, dist INT, second INT, candidates INT, applied_at TEXT);
"""


# ------------------------------------------------------------------ pure parts

def group_key(filename):
    """Base name used to pair candidates: lowercase stem without a trailing '(N)'."""
    stem = os.path.splitext(filename or "")[0].lower()
    return re.sub(r"\(\d+\)$", "", stem)


def assign(takeout, smug, match_max=MATCH_MAX, margin_min=MARGIN_MIN, review_max=REVIEW_MAX):
    """takeout: {item_id: (group, [4 rotation hashes])}; smug: {image_key: (group, hash)}.

    Returns {item_id: dict(decision, image_key, dist, second, candidates)}.
    """
    by_group = {}
    for key, (grp, h) in smug.items():
        by_group.setdefault(grp, []).append((key, h))
    scored = {}
    for item_id, (grp, hs) in takeout.items():
        cands = sorted((min(hamming(x, h) for x in hs), key) for key, h in by_group.get(grp, []))
        scored[item_id] = cands

    out = {}
    used = set()
    clean = sorted((c[0][0], item_id) for item_id, c in scored.items()
                   if c and c[0][0] <= match_max and (len(c) < 2 or c[1][0] >= margin_min))
    for dist, item_id in clean:
        key = scored[item_id][0][1]
        if key in used:
            continue
        used.add(key)
        c = scored[item_id]
        out[item_id] = {"decision": "match", "image_key": key, "dist": dist,
                        "second": c[1][0] if len(c) > 1 else None, "candidates": len(c)}
    for item_id, c in scored.items():
        if item_id in out:
            continue
        best = c[0] if c else None
        decision = "review" if best and best[0] <= review_max else "none"
        out[item_id] = {"decision": decision, "image_key": best[1] if best and decision == "review" else None,
                        "dist": best[0] if best else None, "second": c[1][0] if len(c) > 1 else None,
                        "candidates": len(c)}
    return out


def assign_bursts(takeout, smug, max_dist=MATCH_MAX):
    """One-to-one pairing for near-identical burst frames, where only the moment (month) matters.

    takeout: {item_id: [4 rotation hashes]}; smug: {image_key: hash}. Pairs are taken closest-first with
    no margin requirement; each side is used at most once. Returns ({item_id: (image_key, dist)}, unpaired_ids).
    """
    pairs = sorted((min(hamming(x, h) for x in hs), item_id, key)
                   for item_id, hs in takeout.items() for key, h in smug.items())
    used_items, used_keys, out = set(), set(), {}
    for dist, item_id, key in pairs:
        if dist > max_dist:
            break
        if item_id in used_items or key in used_keys:
            continue
        used_items.add(item_id)
        used_keys.add(key)
        out[item_id] = (key, dist)
    return out, sorted(set(takeout) - used_items)


# ------------------------------------------------------------------- I/O parts

def render_hashes(data, ext):
    """Bytes of a still -> 4 rotation dHashes (any format Pillow + pillow-heif can open)."""
    return rotation_hashes(render_small(data))


def hash_takeout(idx, takeout_dir, wanted, workers):
    """Stream archives once, hashing the wanted members in a worker pool. wanted: {(archive, path): item_id}."""
    todo = {k: v for k, v in wanted.items()
            if not idx.execute("SELECT 1 FROM phash_takeout WHERE item_id=?", (v,)).fetchone()}
    log.info("takeout stills to hash: %d (%d cached)", len(todo), len(wanted) - len(todo))
    if not todo:
        return
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for archive in sorted({a for a, _ in todo}):
            futures = {}
            names = {p for a, p in todo if a == archive}
            for path, data in read_members(os.path.join(takeout_dir, archive), names):
                futures[pool.submit(render_hashes, data, os.path.splitext(path)[1].lower())] = todo[(archive, path)]
            for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                item_id = futures[fut]
                try:
                    hs = fut.result()
                    idx.execute("INSERT OR REPLACE INTO phash_takeout VALUES(?,?,?,?,?,NULL)",
                                (item_id, *[f"{h:016x}" for h in hs]))
                except Exception as e:
                    idx.execute("INSERT OR REPLACE INTO phash_takeout(item_id, error) VALUES(?,?)", (item_id, repr(e)))
                if n % 500 == 0:
                    idx.commit()
                    log.info("%s: hashed %d/%d", archive, n, len(futures))
            idx.commit()


def hash_smug(idx, client, keys, workers):
    todo = [k for k in keys if not idx.execute("SELECT 1 FROM phash_smug WHERE image_key=?", (k,)).fetchone()]
    log.info("SmugMug images to hash: %d (%d cached)", len(todo), len(keys) - len(todo))

    def one(key):
        return dhash(Image.open(io.BytesIO(client.preview_bytes(key))))

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(one, k): k for k in todo}
        for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            k = futures[fut]
            try:
                idx.execute("INSERT OR REPLACE INTO phash_smug VALUES(?,?,NULL)", (k, f"{fut.result():016x}"))
            except Exception as e:
                idx.execute("INSERT OR REPLACE INTO phash_smug(image_key, error) VALUES(?,?)", (k, repr(e)))
            if n % 200 == 0:
                idx.commit()
                log.info("SmugMug hashed %d/%d", n, len(todo))
    idx.commit()


def select_sets(idx):
    """Unlinked SmugMug keepers (photos) and unlinked Takeout stills sharing a group key with them."""
    smug_rows = idx.execute(
        "SELECT i.image_key, i.filename FROM c.images i JOIN c.plan p USING(image_key) JOIN c.matches m USING(image_key) "
        "WHERE p.status='done' AND p.action='move' AND i.is_video=0 AND (m.google_id IS NULL OR m.google_id NOT IN "
        "(SELECT google_id FROM items WHERE google_id IS NOT NULL AND smug_image_key IS NOT NULL))").fetchall()
    smug = {k: group_key(fn) for k, fn in smug_rows}
    groups = set(smug.values())
    rows = idx.execute(
        "SELECT item_id, archive, path, filename FROM items WHERE status='missing_on_smugmug' AND path IS NOT NULL "
        f"AND lower(ext) IN ({','.join('?' * len(STILL_EXTS))})", STILL_EXTS).fetchall()
    takeout = {r[0]: (r[1], r[2], group_key(r[3])) for r in rows if group_key(r[3]) in groups}
    return smug, takeout


def cmd_compute(idx, args):
    smug, takeout = select_sets(idx)
    log.info("unlinked SmugMug photos: %d; Takeout stills with a same-name candidate: %d", len(smug), len(takeout))
    client = context.client(args.settings)
    hash_smug(idx, client, list(smug), args.workers)
    hash_takeout(idx, args.takeout_dir, {(a, p): i for i, (a, p, _) in takeout.items()}, args.workers)

    th = {r[0]: (takeout[r[0]][2], [int(x, 16) for x in r[1:5]])
          for r in idx.execute("SELECT item_id, h0, h1, h2, h3 FROM phash_takeout WHERE error IS NULL")
          if r[0] in takeout}
    sh = {r[0]: (smug[r[0]], int(r[1], 16))
          for r in idx.execute("SELECT image_key, h FROM phash_smug WHERE error IS NULL") if r[0] in smug}
    result = assign(th, sh)
    with idx:
        idx.execute("DELETE FROM content_matches WHERE applied_at IS NULL")
        for item_id, r in result.items():
            if idx.execute("SELECT 1 FROM content_matches WHERE item_id=? AND applied_at IS NOT NULL", (item_id,)).fetchone():
                continue
            idx.execute("INSERT OR REPLACE INTO content_matches VALUES(?,?,?,?,?,?,NULL)",
                        (item_id, r["decision"], r["image_key"], r["dist"], r["second"], r["candidates"]))
    errors = (idx.execute("SELECT COUNT(*) FROM phash_takeout WHERE error IS NOT NULL").fetchone()[0],
              idx.execute("SELECT COUNT(*) FROM phash_smug WHERE error IS NOT NULL").fetchone()[0])
    summary = dict(idx.execute("SELECT decision, COUNT(*) FROM content_matches GROUP BY 1").fetchall())
    summary["hash_errors_takeout_smug"] = errors
    return summary


def cmd_apply(idx, args):
    """Record clean matches: Takeout item -> SmugMug image; date the SmugMug copy and re-target it to its month."""
    cfg = args.settings
    st = State(args.state)
    st.start_run("content_match.apply", vars(args))
    rows = idx.execute("SELECT cm.item_id, cm.image_key, cm.dist, it.google_id, it.taken_ts FROM content_matches cm "
                       "JOIN items it USING(item_id) WHERE cm.decision='match' AND cm.applied_at IS NULL").fetchall()
    retargeted = 0
    for item_id, image_key, dist, google_id, taken_ts in rows:
        import datetime
        ts_utc = datetime.datetime.fromtimestamp(taken_ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        local = planning.to_local(ts_utc, cfg["timezone"])
        is_video = st.one("SELECT is_video FROM images WHERE image_key=?", image_key)
        target, kind = planning.base_target(is_video, local, cfg)
        st.db.execute("UPDATE matches SET google_id=COALESCE(?, google_id), method='content_dhash', confidence='high', "
                      "capture_ts_utc=?, capture_local=?, notes=? WHERE image_key=?",
                      (google_id, ts_utc, local, f"takeout item {item_id}, dHash distance {dist}", image_key))
        prior = st.q("SELECT target_name, status FROM plan WHERE image_key=?", image_key)[0]
        if prior["target_name"] != target:
            st.db.execute("INSERT OR IGNORE INTO targets(name, kind, planned) VALUES(?,?,0)", (target, kind))
            st.db.execute("UPDATE plan SET target_name=?, status='pending', reason=?, updated_at=? WHERE image_key=?",
                          (target, f"dated via Takeout content match (was {prior['target_name']})", now(), image_key))
            retargeted += 1
        st.event("content_match_applied", image_key=image_key, commit=False, item_id=item_id, dist=dist,
                 target=target, previous=prior["target_name"])
        idx.execute("UPDATE items SET status='on_smugmug_content_match', smug_image_key=?, smug_album=?, "
                    "smug_match_method='content_dhash', smug_confidence='high' WHERE item_id=?",
                    (image_key, target, item_id))
        idx.execute("UPDATE content_matches SET applied_at=? WHERE item_id=?", (now(), item_id))
    st.db.execute("UPDATE targets SET planned=(SELECT COUNT(*) FROM plan WHERE target_name=targets.name)")
    st.db.commit()
    idx.commit()
    summary = {"applied": len(rows), "retargeted_to_month": retargeted}
    st.finish_run("ok", summary)
    return summary


def cmd_burst_apply(idx, args):
    """Resolve 'review' items of a burst-prone name group (default lp_image): pair frames one-to-one,
    date + re-target the paired SmugMug copies, and release unpaired Takeout stills for upload."""
    import datetime
    cfg = args.settings
    st = State(args.state)
    st.start_run("content_match.burst_apply", vars(args))
    grp = args.burst_group
    tk = {r[0]: [int(x, 16) for x in r[1:5]] for r in idx.execute(
        "SELECT cm.item_id, ph.h0, ph.h1, ph.h2, ph.h3 FROM content_matches cm JOIN items it USING(item_id) "
        "JOIN phash_takeout ph USING(item_id) WHERE cm.decision='review' AND cm.applied_at IS NULL")
        if group_key(idx.execute("SELECT filename FROM items WHERE item_id=?", (r[0],)).fetchone()[0]) == grp}
    sm = {r[0]: int(r[1], 16) for r in idx.execute(
        "SELECT s.image_key, s.h FROM phash_smug s JOIN c.plan p USING(image_key) JOIN c.images i USING(image_key) "
        "WHERE s.h IS NOT NULL AND p.status='done' AND p.target_name LIKE '%Undated'")
        if group_key(st.one("SELECT filename FROM images WHERE image_key=?", r[0])) == grp}
    paired, unpaired = assign_bursts(tk, sm)
    for item_id, (image_key, dist) in paired.items():
        taken_ts, google_id = idx.execute("SELECT taken_ts, google_id FROM items WHERE item_id=?", (item_id,)).fetchone()
        ts_utc = datetime.datetime.fromtimestamp(taken_ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        local = planning.to_local(ts_utc, cfg["timezone"])
        target, kind = planning.base_target(False, local, cfg)
        st.db.execute("UPDATE matches SET google_id=COALESCE(?, google_id), method='content_burst', confidence='low', "
                      "capture_ts_utc=?, capture_local=?, notes=? WHERE image_key=?",
                      (google_id, ts_utc, local, f"burst frame ~ takeout item {item_id}, dHash {dist}", image_key))
        st.db.execute("INSERT OR IGNORE INTO targets(name, kind, planned) VALUES(?,?,0)", (target, kind))
        st.db.execute("UPDATE plan SET target_name=?, status='pending', reason=?, updated_at=? WHERE image_key=?",
                      (target, f"dated via burst sibling in Takeout (dHash {dist})", now(), image_key))
        st.db.execute("UPDATE uploads SET status='skipped', reason=?, updated_at=? WHERE item_id=? AND role='still' "
                      "AND status='needs_review'", (f"burst frame already on SmugMug as {image_key}", now(), item_id))
        idx.execute("UPDATE items SET status='on_smugmug_burst', smug_image_key=?, smug_album=?, "
                    "smug_match_method='content_burst', smug_confidence='low' WHERE item_id=?", (image_key, target, item_id))
        idx.execute("UPDATE content_matches SET decision='burst_match', image_key=?, dist=?, applied_at=? "
                    "WHERE item_id=?", (image_key, dist, now(), item_id))
    for item_id in unpaired:
        st.db.execute("UPDATE uploads SET status='planned', reason='burst frame with no SmugMug copy left to pair', "
                      "updated_at=? WHERE item_id=? AND role='still' AND status='needs_review'", (now(), item_id))
        idx.execute("UPDATE content_matches SET decision='burst_unpaired', applied_at=? WHERE item_id=?", (now(), item_id))
    st.db.execute("UPDATE targets SET planned=(SELECT COUNT(*) FROM plan WHERE target_name=targets.name)")
    st.event("burst_apply", commit=False, group=grp, paired=len(paired), unpaired=len(unpaired),
             smug_candidates=len(sm))
    st.db.commit()
    idx.commit()
    summary = {"group": grp, "takeout_review_items": len(tk), "smug_undated_candidates": len(sm),
               "paired_and_retargeted": len(paired), "released_for_upload": len(unpaired)}
    st.finish_run("ok", summary)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm content-match", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    context.add_args(p, paths=("index", "state", "takeout_dir"))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--burst-apply", action="store_true", help="resolve burst-frame review items (see cmd_burst_apply)")
    p.add_argument("--burst-group", default="lp_image")
    args = p.parse_args(argv)
    args.settings = context.resolve(args)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    for noisy in ("urllib3", "requests_oauthlib", "oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    idx = sqlite3.connect(args.index)
    idx.executescript(SCHEMA)
    idx.execute(f"ATTACH '{os.path.abspath(args.state)}' AS c")
    if args.burst_apply:
        summary = cmd_burst_apply(idx, args)
    elif args.apply:
        summary = cmd_apply(idx, args)
    else:
        summary = cmd_compute(idx, args)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
