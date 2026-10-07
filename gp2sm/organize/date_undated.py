"""Give items in the undated albums a date from evidence, and move them to dated albums.

Evidence chain (the first confident answer wins; the method is recorded per item):
  1. own_time         new uploads: a clip's own capture time, or the still's Takeout capture date
  2. name_month       every same-named Takeout candidate falls in one month
  3. video_shape      exactly one same-named Takeout video matches duration (±max(0.5 s, 7%)) and aspect ratio, and
                      isn't lower-resolution than the destination copy (destinations don't upscale). Identical
                      twins are paired one-to-one. video_shape_all: the same test against every Takeout video.
  4. content_name     perceptual hash (dHash) of the photo vs same-named Takeout photos: best <= 6, next >= 12;
                      content_month: several near-identical matches (<= 6) that all fall in one month
  5. content_all      (opt-in, --full-scan) the same test against every Takeout still, for items with no name match

Moves are reversible. Live Photo clips and stills go to photo month albums; regular videos go to video albums.

  gp2sm date-undated plan [--full-scan]
  gp2sm date-undated apply
  gp2sm date-undated report
"""

import argparse
import datetime
import io
import logging
import os
import re
import sqlite3
import sys
import tarfile
from zoneinfo import ZoneInfo

from PIL import Image

from gp2sm.cli import run
from gp2sm.media import aspect, mp4_dims, mp4_duration
from gp2sm.organize import planning
from gp2sm.organize.consolidate import ensure_target, setup_logging
from gp2sm.organize.content_match import MARGIN_MIN, MATCH_MAX, dhash, hamming, hash_takeout
from gp2sm.project import context
from gp2sm.smugmug.client import SmugMugError
from gp2sm.state import State, now
from gp2sm.takeout.upload import clip_creation_ts

log = logging.getLogger("gp2sm.organize.date_undated")
VIDEO_EXTS = (".mp4", ".mov")


# ------------------------------------------------------------------ pure parts

def stem_key(filename):
    """Lowercase base name without '(N)' counters or the legacy tool's 'dl_<digits>_' download prefix."""
    stem = os.path.splitext(filename or "")[0].lower()
    stem = re.sub(r"^dl_\d+_", "", stem)
    return re.sub(r"\(\d+\)$", "", stem)


def single_month(timestamps, tz_name):
    """The month shared by all timestamps, else None."""
    months = {datetime.datetime.fromtimestamp(t, ZoneInfo(tz_name)).strftime("%Y-%m") for t in timestamps if t}
    return months.pop() if len(months) == 1 else None


def video_shape_hits(target, candidates, dur_tol=0.5, dur_rel=0.07, ratio_tol=0.02, upscale_slack=0.95):
    """target: (duration_s, ratio, pixels); candidates: [(id, duration_s, ratio, pixels)]. All matching ids.

    Duration within max(dur_tol, dur_rel * duration) (re-encoding pads length), same aspect ratio, and the
    candidate must not be lower-resolution than the destination copy (destinations don't upscale)."""
    dur, ratio, pixels = target
    if dur is None or ratio is None:
        return []
    tol = max(dur_tol, dur_rel * dur)
    return [cid for cid, d, r, px in candidates
            if d is not None and r is not None and abs(d - dur) <= tol and abs(r - ratio) / r <= ratio_tol
            and not (pixels and px and px < upscale_slack * pixels)]


def pick_by_video_shape(target, candidates, **kw):
    """The unique video_shape match, else None."""
    hits = video_shape_hits(target, candidates, **kw)
    return hits[0] if len(hits) == 1 else None


def near_matches(smug_hash, candidates, match_max=MATCH_MAX):
    """Candidate ids within match_max (near-identical frames)."""
    return [cid for cid, hs in candidates.items() if min(hamming(h, smug_hash) for h in hs) <= match_max]


def pick_by_content(smug_hash, candidates, match_max=MATCH_MAX, margin_min=MARGIN_MIN):
    """candidates: {id: [4 rotation hashes]}. Best match if <= match_max and next-best >= margin_min."""
    scored = sorted((min(hamming(h, smug_hash) for h in hs), cid) for cid, hs in candidates.items())
    if not scored or scored[0][0] > match_max:
        return None, (scored[0][0] if scored else None)
    if len(scored) > 1 and scored[1][0] < margin_min:
        return None, scored[0][0]
    return scored[0][1], scored[0][0]


# ------------------------------------------------------------------- planning

def month_to_target(cfg, ts, is_video_album):
    local = planning.to_local(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(), cfg["timezone"])
    return planning.base_target(is_video_album, local, cfg)[0], local


def cmd_plan(st, cfg, client, args):
    undated = (cfg["undated_photo_album"], cfg["undated_video_album"])
    idx = sqlite3.connect(f"file:{os.path.abspath(args.index)}?mode=ro", uri=True)
    idx.row_factory = sqlite3.Row
    items = [dict(r) for r in idx.execute("SELECT item_id, archive, path, filename, lower(ext) ext, taken_ts, "
                                          "motion_path FROM items WHERE path IS NOT NULL AND taken_ts IS NOT NULL")]
    by_stem = {}
    for it in items:
        by_stem.setdefault(stem_key(it["filename"]), []).append(it)
    done_refs = {(r[0], r[1]) for r in st.q("SELECT kind, ref_id FROM datings WHERE status IN ('planned','done')")}
    st.db.execute("DELETE FROM datings WHERE status='unresolved'")  # re-evaluate with current rules
    st.db.commit()  # never hold the write lock across the long, network-bound planning below
    rows = []

    # (1) uploads sitting in an undated album, not already being placed beside a still
    for r in st.q("SELECT u.* FROM uploads u LEFT JOIN placements p ON p.upload_id=u.upload_id AND p.status!='failed' "
                  "WHERE u.status='done' AND u.target_name IN (?,?) AND p.placement_id IS NULL", *undated):
        r = dict(r)
        if ("upload", str(r["upload_id"])) in done_refs:
            continue
        ts, method = None, None
        if r["role"] == "clip" and r["staged_path"] and os.path.exists(r["staged_path"]):
            ts, method = clip_creation_ts(open(r["staged_path"], "rb").read()), "own_time"
        if not ts:
            ts = idx.execute("SELECT taken_ts FROM items WHERE item_id=?", (r["pair_item_id"] or r["item_id"],)).fetchone()
            ts, method = (ts[0] if ts else None), "own_time"
        rows.append({"kind": "upload", "ref_id": str(r["upload_id"]), "item_id": r["image_key"], "serial": 0,
                     "name": r["upload_name"], "is_video": int(r["role"] == "clip"), "role": r["role"],
                     "from_album": r["target_name"], "ts": ts, "method": method if ts else None,
                     "evidence": "clip/still timestamp" if ts else "no timestamp"})

    # (2) earlier consolidation copies sitting in an undated album
    images = [dict(r) for r in st.q("SELECT i.image_key, i.serial, i.filename, i.is_video, i.width, i.height, i.duration_s, "
                                     "p.target_name FROM plan p JOIN images i USING(image_key) WHERE p.status='done' "
                                     "AND p.action='move' AND p.target_name IN (?,?)", *undated)]
    need_hash, need_video = {}, {}
    for im in images:
        if ("image", im["image_key"]) in done_refs:
            continue
        cands = [c for c in by_stem.get(stem_key(im["filename"]), []) if (c["ext"] in VIDEO_EXTS) == bool(im["is_video"])]
        row = {"kind": "image", "ref_id": im["image_key"], "item_id": im["image_key"], "serial": im["serial"],
               "name": im["filename"], "is_video": im["is_video"], "role": "video" if im["is_video"] else "still",
               "from_album": im["target_name"], "ts": None, "method": None, "evidence": f"{len(cands)} same-name candidates"}
        m = single_month([c["taken_ts"] for c in cands], cfg["timezone"]) if cands else None
        if m:
            row.update(ts=min(c["taken_ts"] for c in cands), method="name_month")
        elif cands and im["is_video"]:
            need_video[im["image_key"]] = (im, cands)
        elif cands:
            need_hash[im["image_key"]] = (im, cands)
        elif args.full_scan and not im["is_video"]:
            need_hash[im["image_key"]] = (im, None)
        rows.append(row)
    by_ref = {(r["kind"], r["ref_id"]): r for r in rows}

    # (3) videos: duration + shape against same-named Takeout videos (then all Takeout videos)
    if need_video:
        all_videos = {it["item_id"]: it for it in items if it["ext"] in VIDEO_EXTS and not it["motion_path"]}
        wanted = {(c["archive"], c["path"]): c for c in all_videos.values()}
        info = {}
        log.info("reading headers of %d Takeout videos", len(wanted))
        for archive in sorted({a for a, _ in wanted}):
            with tarfile.open(os.path.join(args.takeout_dir, archive), "r|*") as tar:
                for m in tar:
                    c = wanted.get((archive, m.name))
                    if c:
                        data = tar.extractfile(m).read()
                        dims = mp4_dims(data)
                        info[c["item_id"]] = (mp4_duration(data), aspect(*dims) if dims else None,
                                              dims[0] * dims[1] if dims else None)
        taken_by = {}  # one-to-one: identical twins can each be claimed only once
        for key, (im, cands) in sorted(need_video.items(), key=lambda kv: kv[1][0]["filename"]):
            v = client.video_info(key, im["serial"])
            w, h = v["width"] or im["width"], v["height"] or im["height"]
            target = (v["duration_s"] or im["duration_s"], aspect(w, h) if w and h else None, w * h if w and h else None)
            row = by_ref[("image", key)]
            method, hits = "video_shape", video_shape_hits(target, [(c["item_id"], *info.get(c["item_id"], (None,) * 3))
                                                                    for c in cands])
            if not hits:
                method = "video_shape_all"
                hits = video_shape_hits(target, [(iid, *info.get(iid, (None,) * 3)) for iid in all_videos])
            free = [h for h in hits if h not in taken_by]
            twins = len({info.get(h)[:2] for h in hits}) == 1 if hits else False
            pick = free[0] if (len(hits) == 1 or (twins and free)) and free else None
            if pick:
                taken_by[pick] = key
                row.update(ts=all_videos[pick]["taken_ts"], method=method + ("_twin" if len(hits) > 1 else ""),
                           evidence=f"duration {target[0]}s / aspect {target[1]:.2f} -> {all_videos[pick]['filename']}")
            else:
                row["evidence"] = f"video shape: {len(hits)} matching candidates (needs 1)"

    # (4/5) photos: content hash against same-named (or, with --full-scan, all) Takeout stills
    if need_hash:
        idx_w = sqlite3.connect(args.index)
        cached = {r[0]: [int(x, 16) for x in r[1:5]] for r in
                  idx_w.execute("SELECT item_id, h0, h1, h2, h3 FROM phash_takeout WHERE error IS NULL")}
        pool = {c["item_id"]: c for _, cands in need_hash.values() for c in (cands or [])}
        if args.full_scan:
            pool.update({it["item_id"]: it for it in items if it["ext"] not in VIDEO_EXTS})
        missing = {(c["archive"], c["path"]): iid for iid, c in pool.items() if iid not in cached}
        log.info("hashing %d Takeout stills not already cached (parallel, cached for next time)", len(missing))
        hash_takeout(idx_w, args.takeout_dir, missing, args.workers)
        cached = {r[0]: [int(x, 16) for x in r[1:5]] for r in
                  idx_w.execute("SELECT item_id, h0, h1, h2, h3 FROM phash_takeout WHERE error IS NULL")}
        all_stills = {it["item_id"]: it for it in items if it["ext"] not in VIDEO_EXTS}
        for key, (im, cands) in need_hash.items():
            h = dhash(Image.open(io.BytesIO(client.preview_bytes(key, im["serial"]))))
            row = by_ref[("image", key)]
            tries = [("content_name", cands)] if cands else []
            if args.full_scan:
                tries.append(("content_all", None))
            for method, cset in tries:
                src = cset if cset is not None else all_stills.values()
                pool_c = {c["item_id"]: cached[c["item_id"]] for c in src if c["item_id"] in cached}
                pick, best = pick_by_content(h, pool_c)
                if pick:
                    row.update(ts=all_stills[pick]["taken_ts"], method=method, evidence=f"dHash {best} vs {len(pool_c)}")
                    break
                near = near_matches(h, pool_c)
                month = single_month([all_stills[n]["taken_ts"] for n in near], cfg["timezone"]) if near else None
                if month:
                    row.update(ts=min(all_stills[n]["taken_ts"] for n in near), method=method.replace("content", "content_month"),
                               evidence=f"{len(near)} near-identical matches, all in {month}")
                    break
                row["evidence"] = f"no confident content match (best {best} vs {len(pool_c)} candidates)"

    planned = unresolved = 0
    with st.db:
        for r in rows:
            if r["ts"]:
                target, local = month_to_target(cfg, r["ts"], bool(r["is_video"]) and r["role"] == "video")
                status = "planned" if target != r["from_album"] else "unresolved"
            else:
                target, local, status = None, None, "unresolved"
            st.db.execute("INSERT OR REPLACE INTO datings(kind, ref_id, item_id, serial, name, is_video, role, from_album, "
                          "method, capture_ts, capture_local, target_name, evidence, status, updated_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (r["kind"], r["ref_id"], r["item_id"], r["serial"], r["name"], r["is_video"], r["role"],
                           r["from_album"], r["method"], r["ts"], local, target, r["evidence"], status, now()))
            planned += status == "planned"
            unresolved += status == "unresolved"
        st.event("datings_planned", commit=False, planned=planned, unresolved=unresolved)
    return {"planned": planned, "unresolved": unresolved,
            "by_method": dict(st.q("SELECT method, COUNT(*) FROM datings WHERE status='planned' GROUP BY 1"))}


def cmd_apply(st, cfg, client, args):
    rows = [dict(r) for r in st.q("SELECT * FROM datings WHERE status='planned' ORDER BY target_name, name")]
    if not args.yes:
        for r in rows:
            print(f"  would move {r['name']} -> {r['target_name']} ({r['method']})")
        print(f"  {len(rows)} items into {len({r['target_name'] for r in rows})} albums")
        run.dry_run_footer()
        return None
    if not rows:
        return {"moved": 0}
    run.install_sigint()
    progress = run.Progress(len(rows), "dating")
    folder = client.ensure_folder_path(client.root_folder(), cfg["target_folder"])
    album_of = {name: st.one("SELECT album_key FROM targets WHERE name=?", name) for name in {r["from_album"] for r in rows}}
    out = {"moved": 0, "failed": 0}
    for target in sorted({r["target_name"] for r in rows}):
        st.db.execute("INSERT OR IGNORE INTO targets(name, kind, planned) VALUES(?,?,0)",
                      (target, "video" if "Videos" in target else "photo"))
        dest = ensure_target(st, cfg, client, folder, target)
        st.db.commit()
        for r in [x for x in rows if x["target_name"] == target]:
            if run.Stop.requested:
                break
            ref = client.item_ref(album_of[r["from_album"]], r["item_id"], r["serial"])
            try:
                client.move_items(dest, [ref])
                # Never trust a successful response alone: a move has been seen to return OK yet not happen.
                ok = dest in client.item_album_ids(r["item_id"], r["serial"])
                if not ok:
                    st.db.execute("UPDATE datings SET status='failed', last_error=?, updated_at=? WHERE dating_id=?",
                                  ("move returned OK but the item is not in the target album", now(), r["dating_id"]))
                    out["failed"] += 1
            except SmugMugError as e:
                ok = dest in (client.item_album_ids(r["item_id"], r["serial"]) if e.ambiguous or e.http_status == 400 else [])
                if not ok:
                    st.db.execute("UPDATE datings SET status='failed', last_error=?, updated_at=? WHERE dating_id=?",
                                  (str(e)[:300], now(), r["dating_id"]))
                    out["failed"] += 1
            if ok:
                if r["kind"] == "upload":
                    st.db.execute("UPDATE uploads SET target_name=?, album_image_uri=?, verified_at=NULL, "
                                  "reason=reason || ' -> dated via ' || ? WHERE upload_id=?",
                                  (target, client.item_ref(dest, r["item_id"], r["serial"]), r["method"], int(r["ref_id"])))
                else:
                    st.db.execute("UPDATE plan SET target_name=?, reason=reason || ' -> dated via ' || ?, updated_at=? "
                                  "WHERE image_key=?", (target, r["method"], now(), r["item_id"]))
                    st.db.execute("UPDATE images SET current_album_key=? WHERE image_key=?", (dest, r["item_id"]))
                    st.db.execute("UPDATE matches SET method=?, confidence='medium', capture_ts_utc=?, capture_local=? "
                                  "WHERE image_key=?", (r["method"], datetime.datetime.fromtimestamp(
                                      r["capture_ts"], datetime.timezone.utc).isoformat(), r["capture_local"], r["item_id"]))
                st.db.execute("UPDATE datings SET status='done', updated_at=? WHERE dating_id=?", (now(), r["dating_id"]))
                st.event("item_dated", commit=False, item=r["item_id"], method=r["method"], target=target)
                out["moved"] += 1
            st.db.commit()
            progress.update(failed=int(not ok))
        log.info("-> %s done", target)
    progress.close()
    return out


def cmd_report(st, cfg, client, args):
    for title, sql in (("Datings by status/method", "SELECT status, method, role, COUNT(*) FROM datings GROUP BY 1,2,3"),
                       ("Unresolved", "SELECT name, role, evidence FROM datings WHERE status='unresolved' LIMIT 40")):
        print(f"\n== {title} ==")
        for r in st.q(sql):
            print("  " + "  ".join(str(x) for x in r))


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm date-undated", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["plan", "apply", "report"])
    context.add_args(p, paths=("index", "takeout_dir"))
    p.add_argument("--full-scan", action="store_true", help="plan: content-match name-less photos against all Takeout stills")
    p.add_argument("--workers", type=int, default=8)
    run.add_yes(p, "move the items (apply)")
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = context.client(cfg) if args.command != "report" else None
    lock = None
    if args.command == "apply" and args.yes:
        lock = context.acquire_lock(cfg["state_db"], "date-undated apply")
    command = {"plan": cmd_plan, "apply": cmd_apply, "report": cmd_report}[args.command]
    return run.run_command(st, f"date_undated.{args.command}", args, lambda: command(st, cfg, client, args), lock=lock)


if __name__ == "__main__":
    sys.exit(main())
