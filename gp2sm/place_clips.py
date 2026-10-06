"""Place unsorted Live Photo clips beside their still, by capture time and aspect ratio.

A clip whose still couldn't be identified by name (e.g. reused `lp_image(N)` names) can still be placed:
its own capture time matches its still's to the second, and the clip has the still's shape. Clips are
assigned to stills one-to-one (closest in time first; ties broken by name order, so bursts stay in sequence).

The destination may not support renaming files (SmugMug ignores FileName changes), so placing a clip
**replaces** it: upload it again under the still's name into the still's album, verify the new copy, then
remove the old copy. Removal is the only irreversible step and needs --yes.

Source for the re-upload, in order: the staged copy → re-extracted from the Takeout → downloaded from the
destination (last resort; a re-encoded rendition, not the original).

  python -m gp2sm.place_clips plan      [--album NAME] [--window 60] [--aspect-tol 0.02]
  python -m gp2sm.place_clips upload    [--limit N]
  python -m gp2sm.place_clips verify
  python -m gp2sm.place_clips finalize  [--yes]       # removes the old copies (dry run without --yes)
  python -m gp2sm.place_clips report
"""

import argparse
import concurrent.futures
import datetime
import json
import logging
import os
import sqlite3
import sys
import tarfile

from PIL import Image

from gp2sm.consolidate import load_config, setup_logging
from gp2sm.media import aspect, mp4_dims
from gp2sm.smugmug_client import NotFound, SmugMugClient, SmugMugError
from gp2sm.state import State, now
from gp2sm.takeout_upload import clip_creation_ts, unique_name

log = logging.getLogger("gp2sm.place_clips")


# ------------------------------------------------------------------ pure part

def assign(clips, stills, window=60, aspect_tol=0.02):
    """One-to-one clip -> still assignment by capture time and aspect ratio.

    clips/stills: lists of dict(id, ts, ratio, order). Returns {clip_id: (still_id, seconds_apart, candidates)}.
    Pairs are taken by smallest time gap; equal gaps are taken in (clip order, still order), so a burst of
    clips sharing one second is matched to that second's stills in name order.
    """
    stills = sorted(stills, key=lambda s: s["ts"])
    times = [s["ts"] for s in stills]
    import bisect
    pairs, ncand = [], {}
    for c in clips:
        lo, hi = bisect.bisect_left(times, c["ts"] - window), bisect.bisect_right(times, c["ts"] + window)
        cands = [s for s in stills[lo:hi] if abs(s["ratio"] - c["ratio"]) / s["ratio"] <= aspect_tol]
        ncand[c["id"]] = len(cands)
        pairs += [(abs(s["ts"] - c["ts"]), c["order"], s["order"], c["id"], s["id"]) for s in cands]
    pairs.sort()
    used_c, used_s, out = set(), set(), {}
    for dt, _, _, cid, sid in pairs:
        if cid in used_c or sid in used_s:
            continue
        used_c.add(cid)
        used_s.add(sid)
        out[cid] = (sid, dt, ncand[cid])
    return out


# ------------------------------------------------------------------- helpers

def names_in(st, target):
    s = {r[0].lower() for r in st.q("SELECT i.filename FROM images i JOIN plan p USING(image_key) "
                                    "WHERE p.target_name=? AND p.status='done'", target)}
    s |= {r[0].lower() for r in st.q("SELECT upload_name FROM uploads WHERE target_name=? AND status='done'", target)}
    s |= {r[0].lower() for r in st.q("SELECT new_name FROM placements WHERE new_target=? AND status!='failed'", target)}
    return s


def stem(name):
    return os.path.splitext(name or "")[0]


def candidate_stills(st, cfg, index_db):
    """Stills in dated albums with capture time and shape, excluding ones that already have a clip beside them."""
    undated = {cfg["undated_photo_album"], cfg["undated_video_album"]}
    idx = sqlite3.connect(f"file:{os.path.abspath(index_db)}?mode=ro", uri=True)
    taken = dict(idx.execute("SELECT item_id, taken_ts FROM items"))
    out, has_clip = [], {}
    for target, name in st.q("SELECT target_name, upload_name FROM uploads WHERE role='clip' AND status='done'"):
        has_clip.setdefault(target, set()).add(stem(name).lower())
    for target, name in st.q("SELECT p.target_name, i.filename FROM images i JOIN plan p USING(image_key) "
                             "WHERE p.status='done' AND i.is_video=1"):
        has_clip.setdefault(target, set()).add(stem(name).lower())
    for uid, item_id, path, target, name in st.q(
            "SELECT upload_id, item_id, staged_path, target_name, upload_name FROM uploads "
            "WHERE role='still' AND status='done'"):
        if target in undated or stem(name).lower() in has_clip.get(target, ()) or not taken.get(item_id):
            continue
        try:
            w, h = Image.open(path).size
        except Exception:
            continue
        out.append({"id": f"u{uid}", "ts": taken[item_id], "ratio": aspect(w, h), "target": target, "name": name})
    for key, ts, w, h, target, name in st.q(
            "SELECT i.image_key, m.capture_ts_utc, i.width, i.height, p.target_name, i.filename FROM images i "
            "JOIN matches m USING(image_key) JOIN plan p USING(image_key) WHERE p.status='done' AND p.action='move' "
            "AND i.is_video=0 AND m.capture_ts_utc IS NOT NULL AND i.width AND i.height"):
        if target in undated or stem(name).lower() in has_clip.get(target, ()):
            continue
        t = int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        out.append({"id": f"i{key}", "ts": t, "ratio": aspect(w, h), "target": target, "name": name})
    return out


def resolve_source(row, takeout_dir, client):
    """Bytes + kind for a clip: staged copy, else Takeout, else destination download."""
    if row["staged_path"] and os.path.exists(row["staged_path"]):
        return open(row["staged_path"], "rb").read(), "staged"
    if row["archive"] and row["src_path"]:
        try:
            with tarfile.open(os.path.join(takeout_dir, row["archive"]), "r|*") as tar:
                for m in tar:
                    if m.name == row["src_path"]:
                        return tar.extractfile(m).read(), "takeout"
        except FileNotFoundError:
            pass
    return client.download_item(row["image_key"], is_video=True), "destination"


# ------------------------------------------------------------------ commands

def cmd_plan(st, cfg, client, args):
    album = args.album or cfg["undated_video_album"]
    clips_rows = [dict(r) for r in st.q(
        "SELECT u.* FROM uploads u LEFT JOIN placements pl USING(upload_id) WHERE u.role='clip' AND u.status='done' "
        "AND u.target_name=? AND pl.placement_id IS NULL ORDER BY u.upload_name", album)]
    os.makedirs(args.stage_dir, exist_ok=True)
    clips, meta = [], {}
    for r in clips_rows:
        data, kind = resolve_source(r, args.takeout_dir, client)
        ts, dims = clip_creation_ts(data), mp4_dims(data)
        if not ts or not dims:
            continue
        path = r["staged_path"] if kind == "staged" else os.path.join(args.stage_dir, f"place_{r['upload_id']}.MP4")
        if kind != "staged":
            open(path, "wb").write(data)
        clips.append({"id": r["upload_id"], "ts": ts, "ratio": aspect(*dims), "order": r["upload_name"].lower()})
        meta[r["upload_id"]] = (r, path, kind)
    stills = candidate_stills(st, cfg, args.index)
    for s in stills:
        s["order"] = s["name"].lower()
    result = assign(clips, stills, args.window, args.aspect_tol)
    by_id = {s["id"]: s for s in stills}
    planned = 0
    with st.db:
        for cid, (sid, dt, ncand) in sorted(result.items(), key=lambda x: by_id[x[1][0]]["name"].lower()):
            r, path, kind = meta[cid]
            s = by_id[sid]
            new_name = unique_name(stem(s["name"]) + ".MP4", names_in(st, s["target"]))
            st.db.execute(
                "INSERT INTO placements(upload_id, old_target, old_item_id, old_item_ref, old_name, still_name, "
                "new_target, new_name, seconds_apart, candidates, source_path, source_kind, status, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'planned',?)",
                (cid, r["target_name"], r["image_key"], r["album_image_uri"], r["upload_name"], s["name"],
                 s["target"], new_name, dt, ncand, path, kind, now()))
            planned += 1
        st.event("placements_planned", commit=False, album=album, clips=len(clips_rows), readable=len(clips),
                 planned=planned)
    return {"clips_in_album": len(clips_rows), "with_time_and_shape": len(clips), "stills_considered": len(stills),
            "planned": planned, "left_unplaced": len(clips_rows) - planned}


def cmd_upload(st, cfg, client, args):
    rows = [dict(r) for r in st.q("SELECT * FROM placements WHERE status IN ('planned','unknown') ORDER BY new_target, new_name")]
    if args.limit:
        rows = rows[:args.limit]
    album_ids = {n: st.one("SELECT album_key FROM targets WHERE name=?", n) for n in {r["new_target"] for r in rows}}

    def one(r):
        return client.upload_file(album_ids[r["new_target"]], r["source_path"], r["new_name"], "video/mp4")

    out = {"uploaded": 0, "failed": 0, "unknown": 0}
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(one, r): r for r in rows}
        for fut in concurrent.futures.as_completed(futures):
            r = futures[fut]
            try:
                item = fut.result()
                st.db.execute("UPDATE placements SET status='uploaded', new_item_id=?, new_item_ref=?, last_error=NULL, "
                              "updated_at=? WHERE placement_id=?", (item["item_id"], item["item_ref"], now(), r["placement_id"]))
                out["uploaded"] += 1
            except SmugMugError as e:
                status = "unknown" if e.ambiguous else "failed"
                st.db.execute("UPDATE placements SET status=?, last_error=?, updated_at=? WHERE placement_id=?",
                              (status, str(e)[:500], now(), r["placement_id"]))
                out[status] += 1
            st.db.commit()
    st.event("placements_uploaded", **out)
    return out


def cmd_verify(st, cfg, client, args):
    rows = [dict(r) for r in st.q("SELECT * FROM placements WHERE status IN ('uploaded','unknown')")]
    by_album = {}
    for r in rows:
        by_album.setdefault(r["new_target"], []).append(r)
    out = {"verified": 0, "missing": 0}
    for name, items in sorted(by_album.items()):
        album_id = st.one("SELECT album_key FROM targets WHERE name=?", name)
        server = {i["item_id"]: i for i in client.list_album_items(album_id)}
        by_name = {(i["name"] or "").lower(): i for i in server.values()}
        for r in items:
            hit = server.get(r["new_item_id"]) if r["new_item_id"] else by_name.get(r["new_name"].lower())
            if hit:
                st.db.execute("UPDATE placements SET status='verified', new_item_id=?, new_item_ref=?, updated_at=? "
                              "WHERE placement_id=?", (hit["item_id"], hit["item_ref"], now(), r["placement_id"]))
                out["verified"] += 1
            else:
                st.db.execute("UPDATE placements SET status='planned', last_error='not found on server; will re-upload', "
                              "updated_at=? WHERE placement_id=?", (now(), r["placement_id"]))
                out["missing"] += 1
        st.db.commit()
        log.info("verified %s: %d", name, len(items))
    return out


def cmd_finalize(st, cfg, client, args):
    rows = [dict(r) for r in st.q("SELECT * FROM placements WHERE status='verified'")]
    if not args.yes:
        log.info("dry run: would remove %d old copies from their unsorted album (new copies verified). "
                 "Pass --yes to remove.", len(rows))
        return {"would_remove": len(rows)}
    out = {"removed": 0, "already_gone": 0, "failed": 0}

    def remove(r):  # worker thread: network only, no database access
        try:
            client.remove_item(r["old_item_ref"])
            return r, "removed", None
        except NotFound:
            return r, "already_gone", None
        except SmugMugError as e:
            return r, "failed", str(e)

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for n, (r, result, err) in enumerate(pool.map(remove, rows), 1):
            out[result] += 1
            if result == "failed":
                st.db.execute("UPDATE placements SET last_error=?, updated_at=? WHERE placement_id=?",
                              (f"remove old copy: {err}"[:300], now(), r["placement_id"]))
            else:
                st.db.execute("UPDATE uploads SET target_name=?, upload_name=?, image_key=?, album_image_uri=?, "
                              "verified_at=?, reason=reason || ' -> placed beside ' || ? WHERE upload_id=?",
                              (r["new_target"], r["new_name"], r["new_item_id"], r["new_item_ref"], now(),
                               r["still_name"], r["upload_id"]))
                st.db.execute("UPDATE placements SET status='done', updated_at=? WHERE placement_id=?",
                              (now(), r["placement_id"]))
                st.event("clip_placed", commit=False, upload_id=r["upload_id"], old=r["old_item_ref"],
                         new=r["new_item_ref"], target=r["new_target"], name=r["new_name"])
            st.db.commit()
            if n % 250 == 0:
                log.info("finalize: %d/%d (removed %d, already gone %d, failed %d)", n, len(rows), out["removed"],
                         out["already_gone"], out["failed"])
    for name in {r["new_target"] for r in rows}:
        try:
            client.set_sort_by_filename(st.one("SELECT album_key FROM targets WHERE name=?", name))
        except SmugMugError as e:
            log.warning("could not set filename sort on %s: %s", name, e)
    return out


def cmd_report(st, cfg, client, args):
    for title, sql in (("Placements by status", "SELECT status, COUNT(*), SUM(candidates=1) single_candidate FROM placements GROUP BY 1"),
                       ("Seconds apart", "SELECT seconds_apart, COUNT(*) FROM placements GROUP BY 1 ORDER BY 1 LIMIT 10"),
                       ("Source used", "SELECT source_kind, COUNT(*) FROM placements GROUP BY 1"),
                       ("Top target albums", "SELECT new_target, COUNT(*) FROM placements GROUP BY 1 ORDER BY 2 DESC LIMIT 8")):
        print(f"\n== {title} ==")
        for r in st.q(sql):
            print("  " + "  ".join(str(x) for x in r))


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm.place_clips", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["plan", "upload", "verify", "finalize", "report"])
    p.add_argument("--config", default="data/consolidate.json")
    p.add_argument("--index", default="data/takeout_index.db")
    p.add_argument("--takeout-dir", default="data/takeout")
    p.add_argument("--stage-dir", default="data/stage")
    p.add_argument("--album", help="unsorted album to place clips from (default: undated video album)")
    p.add_argument("--window", type=int, default=60)
    p.add_argument("--aspect-tol", type=float, default=0.02)
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--yes", action="store_true", help="finalize: actually remove the old copies")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = SmugMugClient.from_config_file(cfg["smugmug_config"]) if args.command != "report" else None
    st.start_run(f"place_clips.{args.command}", vars(args))
    try:
        summary = {"plan": cmd_plan, "upload": cmd_upload, "verify": cmd_verify, "finalize": cmd_finalize,
                   "report": cmd_report}[args.command](st, cfg, client, args)
    except BaseException as e:
        st.db.rollback()
        st.event("run_failed", level="error", error=repr(e))
        st.finish_run("failed", {"error": repr(e)})
        raise
    st.finish_run("ok", summary)
    if summary is not None:
        print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
