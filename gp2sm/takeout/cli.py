"""gp2sm takeout: import a Google Takeout into the destination.

  index       read the archives (.zip/.tgz) in the takeout folder (same as `gp2sm takeout-index`)
  inventory   list the destination albums in scope ([takeout] existing; default: the project's folder)
  dedupe      decide, for every Takeout item, whether it's already there: exact | same | new | review
  review      export the unclear cases side by side for a person to sort; `review --read` takes the answers
  plan        decide what to upload, where and under which name (Live Photo clips beside their stills)
  stage       read the planned files from the archives (HEIC/rejected formats converted to JPEG, EXIF kept)
  upload      upload staged files (dry run unless --yes)
  verify      confirm uploads on the destination
  remove      delete specific uploads this tool made, by upload id (dry run unless --yes)
  report      what was planned, uploaded, held for review and skipped, and why

Typical run: index -> inventory -> dedupe -> plan -> stage -> upload --yes -> verify
"""

import argparse
import logging
import os
import sqlite3
import sys

from gp2sm.cli import run
from gp2sm.importer import dedupe, inventory, review
from gp2sm.importer import plan as planner
from gp2sm.project import context
from gp2sm.state import State, now
from gp2sm.takeout import index as takeout_index
from gp2sm.takeout import upload as transfer
from gp2sm.takeout.archive import list_archives
from gp2sm.takeout.source import TakeoutSource

log = logging.getLogger("gp2sm.takeout.cli")


def scope(cfg):
    return cfg["takeout"]["existing"] or [cfg["target_folder"]]


def split_ref(ref):
    archive, path = ref.split("::", 1)
    return archive, path


def source_dicts(source):
    """Planner/dedupe input for every Takeout item (Live Photo clips ride along with their still)."""
    out = []
    for i in source.iter_items(include_orphans=True):
        motion = i.extras.get("motion")
        out.append({"ref": i.source_id, "name": i.name, "kind": i.kind, "md5": i.md5, "taken_ts": i.taken_ts,
                    "own_time": i.own_time, "width": i.width, "height": i.height, "duration_s": i.duration_s,
                    "orphan": bool(i.extras.get("orphan")),
                    "motion": dict(motion, ref=i.motion_ref) if motion else None})
    return out


def cmd_index(st, cfg, client, args):
    archives = [os.path.join(args.takeout_dir, a) for a in list_archives(args.takeout_dir)]
    if not archives:
        raise SystemExit(f"no Takeout archives (.zip/.tgz) in {args.takeout_dir}")
    takeout_index.main(archives + ["--db", args.index])
    db = sqlite3.connect(f"file:{os.path.abspath(args.index)}?mode=ro", uri=True)
    try:
        return {"archives": db.execute("SELECT COUNT(*) FROM archives").fetchone()[0],
                "files": db.execute("SELECT COUNT(*) FROM members").fetchone()[0],
                "unreadable_media": db.execute("SELECT COUNT(*) FROM members WHERE probe_error IS NOT NULL")
                .fetchone()[0]}
    finally:
        db.close()


def cmd_inventory(st, cfg, client, args):
    progress = run.Progress(0, "albums listed")
    out = inventory.refresh(st, client, scope(cfg), workers=args.workers, progress=progress)
    progress.close()
    return out


def cmd_dedupe(st, cfg, client, args):
    if not st.one("SELECT COUNT(*) FROM dest_albums"):
        log.warning("the destination inventory is empty: run `gp2sm takeout inventory` first "
                    "(or the scope %s has no albums yet)", scope(cfg))
    source = TakeoutSource(args.index, args.takeout_dir)
    return dedupe.run(st, client, source_dicts(source), source.iter_bytes_refs, cfg["takeout"], workers=args.workers)


def effective_decisions(st):
    return {r["source_ref"]: {"decision": dedupe.effective_decision(r["decision"], r["reviewed"]),
                              "dest_item_id": r["dest_item_id"]}
            for r in st.q("SELECT source_ref, decision, reviewed, dest_item_id FROM source_matches")}


def dest_snapshot(st):
    items, names, counts = {}, {}, {}
    for r in st.q("SELECT i.item_id, i.name, i.is_video, i.album_id, a.name AS album FROM dest_items i "
                  "JOIN dest_albums a USING(album_id)"):
        items[r["item_id"]] = {"album": r["album"], "album_id": r["album_id"], "name": r["name"],
                               "is_video": r["is_video"]}
        names.setdefault(r["album"], set()).add((r["name"] or "").lower())
        counts[r["album"]] = counts.get(r["album"], 0) + 1
    return {"items": items, "names": names, "counts": counts}


def release_answered_holds(st, decisions):
    """Rows held for review whose item (or whose clip's still) a person has now answered are planned afresh.
    They were never uploaded, so this only changes the plan."""
    held = [dict(r) for r in st.q("SELECT upload_id, source_ref, pair_ref FROM uploads "
                                  "WHERE source_ref IS NOT NULL AND status='needs_review'")]
    answered = [r["upload_id"] for r in held
                if decisions.get(r["pair_ref"] or r["source_ref"], {}).get("decision") != "review"]
    with st.db:
        st.db.executemany("DELETE FROM uploads WHERE upload_id=?", [(u,) for u in answered])
    return len(answered)


def cmd_plan(st, cfg, client, args):
    items = source_dicts(TakeoutSource(args.index, args.takeout_dir))
    decisions = effective_decisions(st)
    released = release_answered_holds(st, decisions)
    if cfg["takeout"]["dedupe"] != "off":
        undecided = [i["ref"] for i in items if i["ref"] not in decisions]
        if undecided:
            raise SystemExit(f"{len(undecided)} Takeout items have no dedupe decision yet: "
                             "run `gp2sm takeout inventory` and `gp2sm takeout dedupe` first")
    previous = {(r["source_ref"], r["role"]): {"target_name": r["target_name"], "upload_name": r["upload_name"]}
                for r in st.q("SELECT source_ref, role, target_name, upload_name FROM uploads "
                              "WHERE source_ref IS NOT NULL")}
    albums_cfg = {"photo": cfg["photo_album_template"], "video": cfg["video_album_template"],
                  "undated_photo": cfg["undated_photo_album"], "undated_video": cfg["undated_video_album"]}
    rows, notes = planner.plan(items, decisions, dest_snapshot(st), previous, cfg["takeout"], albums_cfg,
                               cfg["timezone"], client.capabilities, cfg["album_soft_cap"])
    # albums in the project's folder are found by name; a clip joining its still elsewhere names its album id
    album_ids = dict(st.q("SELECT name, album_id FROM dest_albums WHERE folder=?", cfg["target_folder"]))
    for r in rows:
        if r["album_id"]:
            known = st.one("SELECT album_key FROM targets WHERE name=?", r["target_name"])
            if known and known != r["album_id"]:
                r["status"], r["reason"] = "held", (f"another album named {r['target_name']!r} is already a target; "
                                                    "rename one of them")
            album_ids[r["target_name"]] = r["album_id"]
    kinds = {}
    for r in rows:
        video = r["role"] in ("video", "clip")
        kinds[r["target_name"]] = "video" if video and kinds.get(r["target_name"], "video") == "video" else "photo"
    stamp = now()
    with st.db:
        for r in rows:
            archive, path = split_ref(r["source_ref"])
            st.db.execute(
                "INSERT INTO uploads(source_ref, role, archive, src_path, upload_name, target_name, reason, status, "
                "convert, content_type, taken_ts, pair_ref, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["source_ref"], r["role"], archive, path, r["upload_name"], r["target_name"], r["reason"],
                 r["status"], int(r["convert"]), r["content_type"], r["taken_ts"], r["pair_ref"], stamp))
        for name, kind in sorted(kinds.items()):
            # an album already on the destination (e.g. a still's existing album) is used as is
            st.db.execute("INSERT OR IGNORE INTO targets(name, kind, planned, album_key) VALUES(?,?,0,?)",
                          (name, kind, album_ids.get(name)))
        st.event("takeout_plan", commit=False, rows=len(rows), **{k[:40]: v for k, v in notes.items()})
    counts = {}
    for r in rows:
        key = f"{r['role']}/{r['status']}"
        counts[key] = counts.get(key, 0) + 1
    return {"planned": dict(sorted(counts.items())), "not planned": notes, "review answers applied": released}


def cmd_review(st, cfg, client, args):
    folder = os.path.join(args.project_root or ".", "review")
    if args.read:
        return review.read_answers(st, folder)
    source = TakeoutSource(args.index, args.takeout_dir)
    return review.export(st, client, folder, source.iter_bytes_refs)


def cmd_stage(st, cfg, client, args):
    return transfer.cmd_stage(st, cfg, args)


def cmd_upload(st, cfg, client, args):
    return transfer.cmd_upload(st, cfg, client, args)


def cmd_verify(st, cfg, client, args):
    return transfer.cmd_verify(st, cfg, client, args)


def cmd_remove(st, cfg, client, args):
    if not args.ids:
        raise SystemExit("name the upload ids to remove (see `gp2sm takeout report`)")
    return transfer.cmd_remove(st, cfg, client, args)


def cmd_report(st, cfg, client, args):
    def show(title, sql):
        print(f"\n== {title} ==")
        for r in st.q(sql):
            print("  " + "  ".join(str(x) for x in r))
    show("Already there? (dedupe)", "SELECT decision, COALESCE(reviewed, ''), COUNT(*) FROM source_matches "
                                    "GROUP BY 1, 2 ORDER BY 3 DESC")
    show("Uploads by role and status", "SELECT role, status, COUNT(*) FROM uploads WHERE source_ref IS NOT NULL "
                                       "GROUP BY 1, 2 ORDER BY 1, 2")
    show("Top target albums", "SELECT target_name, COUNT(*) FROM uploads WHERE source_ref IS NOT NULL "
                              "GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
    show("Held for review", "SELECT upload_name, target_name, reason FROM uploads WHERE source_ref IS NOT NULL "
                            "AND status='needs_review' LIMIT 20")


COMMANDS = {"index": cmd_index, "inventory": cmd_inventory, "dedupe": cmd_dedupe, "review": cmd_review, "plan": cmd_plan,
            "stage": cmd_stage, "upload": cmd_upload, "verify": cmd_verify, "remove": cmd_remove,
            "report": cmd_report}
NEEDS_CLIENT = {"inventory", "dedupe", "review", "plan", "upload", "verify", "remove"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm takeout", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    p.add_argument("ids", nargs="*", help="remove: upload ids")
    p.add_argument("--reason", default="removed by request", help="remove: recorded reason")
    context.add_args(p, paths=("index", "takeout_dir", "stage_dir"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--target", action="append", help="stage/upload/verify: only these albums (glob; repeatable)")
    p.add_argument("--limit", type=int, help="stage/upload: at most this many files")
    p.add_argument("--all", action="store_true", help="verify: re-check everything, not just unverified uploads")
    p.add_argument("--read", action="store_true", help="review: read the answers sorted into same/ and different/")
    run.add_yes(p, "upload / remove")
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    run.setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = context.client(cfg) if args.command in NEEDS_CLIENT else None
    lock = None
    if args.command in ("upload", "remove") and args.yes:
        lock = context.acquire_lock(cfg["state_db"], f"takeout {args.command}")
    command = COMMANDS[args.command]
    code = run.run_command(st, f"takeout.{args.command}", args, lambda: command(st, cfg, client, args), lock=lock)
    if args.command == "remove" and not args.yes:
        run.dry_run_footer()
    return code


if __name__ == "__main__":
    sys.exit(main())
