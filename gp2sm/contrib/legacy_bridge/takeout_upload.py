"""The first migration's Takeout upload plan (legacy bridge): Live Photos and HEIC stills only.

Goal per Live Photo: one JPEG and one MP4 clip with the same base name in the same album, which is
sorted by filename so each pair sits together. A still is uploaded only when no SmugMug copy is known
(linked via the legacy transfer database or an applied content match); otherwise only its clip is added
next to the existing JPEG. New imports use `gp2sm takeout plan` instead.

  plan     decide what to upload, target albums and exact filenames (idempotent)
  stage | upload | verify | remove   shared with `gp2sm takeout` (gp2sm/takeout/upload.py)
  report   summarize

Usage: gp2sm takeout-upload [--config data/consolidate.json] <command> [--target GLOB] [--limit N]
"""

import argparse
import datetime
import os
import sqlite3
import sys

from gp2sm.cli import run
from gp2sm.organize import planning
from gp2sm.organize.consolidate import setup_logging
from gp2sm.project import context
from gp2sm.state import State, now
from gp2sm.takeout.upload import cmd_remove, cmd_stage, cmd_upload, cmd_verify, stem_of, unique_name

HEIC = (".heic", ".heif")
ON_SMUGMUG = ("on_smugmug", "on_smugmug_content_match", "on_smugmug_by_hash", "on_smugmug_inferred_same_name_group")


# ------------------------------------------------------------------------ plan

def cmd_plan(st, cfg, args):
    idx = sqlite3.connect(f"file:{os.path.abspath(args.index)}?mode=ro", uri=True)
    idx.row_factory = sqlite3.Row
    items = [dict(r) for r in idx.execute(
        "SELECT it.*, cm.decision AS cm_decision FROM items it LEFT JOIN content_matches cm USING(item_id) "
        "WHERE it.path IS NOT NULL AND (lower(it.ext) IN ('.heic','.heif') OR it.motion_path IS NOT NULL) "
        "AND it.status != 'excluded_by_user'")]
    # a Live Photo's clip can land in a different archive than its still
    motion_archive = dict(idx.execute("SELECT path, archive FROM members WHERE lower(ext) IN ('.mp4','.mov')"))
    existing_target = {r[0]: (r[1], r[2]) for r in st.q(
        "SELECT p.image_key, p.target_name, i.filename FROM plan p JOIN images i USING(image_key)")}
    # names already present (or planned) per target album, for collision-free naming
    taken = {}
    for target, filename in existing_target.values():
        taken.setdefault(target, set()).add((filename or "").lower())
    for r in st.q("SELECT target_name, upload_name FROM uploads"):
        taken.setdefault(r[0], set()).add(r[1].lower())
    already = {(r[0], r[1]) for r in st.q("SELECT item_id, role FROM uploads")}

    def month_target(it):
        ts = it["taken_ts"]
        local = planning.to_local(
            datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(), cfg["timezone"]) if ts else None
        return planning.base_target(False, local, cfg)[0]

    rows = []
    counts = {}
    for it in sorted(items, key=lambda r: (r["taken_ts"] or 0, r["filename"])):
        ext = (it["ext"] or "").lower()
        on_sm = it["status"] in ON_SMUGMUG
        if on_sm and it["smug_image_key"] and it["smug_image_key"] in existing_target:
            target, existing_name = existing_target[it["smug_image_key"]]
            base = stem_of(existing_name)
            still_role = None
        elif on_sm:
            target, base, still_role = month_target(it), stem_of(it["filename"]), None
        else:
            target, base = month_target(it), stem_of(it["filename"])
            still_role = "still" if ext in HEIC else None  # non-HEIC stills not on SmugMug were handled earlier
        review = it["cm_decision"] == "review"
        for role, src, name in ((still_role, it["path"], base + ".JPG"),
                                ("clip" if it["motion_path"] else None, it["motion_path"], base + ".MP4")):
            if not role or (it["item_id"], role) in already:
                continue
            names = taken.setdefault(target, set())
            final = unique_name(name, names)
            names.add(final.lower())
            status = "needs_review" if review else "planned"
            reason = ("still has a near-but-unclear content match; check before uploading" if review else
                      "clip next to existing SmugMug JPEG" if role == "clip" and not still_role else
                      "Live Photo still converted from HEIC" if role == "still" and it["motion_path"] else
                      "HEIC still converted to JPEG" if role == "still" else "Live Photo motion clip")
            archive = it["archive"] if role == "still" else motion_archive.get(src, it["archive"])
            rows.append((it["item_id"], role, archive, src, final, target, reason, status, now()))
            counts[(role, status)] = counts.get((role, status), 0) + 1
    with st.db:
        st.db.executemany("INSERT OR IGNORE INTO uploads(item_id, role, archive, src_path, upload_name, target_name, "
                          "reason, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        for name in {r[5] for r in rows}:
            kind = "photo_undated" if name == cfg["undated_photo_album"] else "photo"
            st.db.execute("INSERT OR IGNORE INTO targets(name, kind, planned) VALUES(?,?,0)", (name, kind))
        st.event("upload_plan", commit=False, rows=len(rows))
    return {f"{r}/{s}": n for (r, s), n in sorted(counts.items())}


def cmd_report(st, cfg, args):
    for title, sql in (
            ("Uploads by role/status", "SELECT role, status, COUNT(*) n, round(SUM(staged_size)/1e9,2) gb "
                                       "FROM uploads GROUP BY 1,2 ORDER BY 1,2"),
            ("EXIF notes (stills)", "SELECT substr(exif_note,1,24) note, COUNT(*) FROM uploads WHERE role='still' "
                                    "GROUP BY 1"),
            ("Top targets by pending work", "SELECT target_name, SUM(status IN ('planned','staged')) todo, "
                                            "SUM(status='done') done FROM uploads GROUP BY 1 ORDER BY 2 DESC LIMIT 10"),
            ("Failures", "SELECT upload_name, target_name, substr(last_error,1,100) FROM uploads "
                         "WHERE status IN ('failed','unknown') LIMIT 15")):
        print(f"\n== {title} ==")
        for r in st.q(sql):
            print("  " + "  ".join(str(x) for x in r))
    return None


# ------------------------------------------------------------------------ main

def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm takeout-upload", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    context.add_args(p, paths=("index", "takeout_dir", "stage_dir"))
    p.add_argument("command", choices=["plan", "stage", "upload", "verify", "report", "remove"])
    p.add_argument("ids", nargs="*", help="remove: upload ids")
    run.add_yes(p, "upload / remove")
    p.add_argument("--reason", default="misplaced", help="remove: recorded reason")
    p.add_argument("--all", action="store_true", help="verify: re-check everything, not just unverified uploads")
    p.add_argument("--target", action="append", help="only these target albums (glob; repeatable)")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = context.client(cfg) if args.command in ("upload", "verify", "remove") else None
    lock = None
    if args.command in ("upload", "remove") and args.yes:
        lock = context.acquire_lock(cfg["state_db"], f"takeout-upload {args.command}")
    commands = {"plan": lambda: cmd_plan(st, cfg, args), "stage": lambda: cmd_stage(st, cfg, args),
                "upload": lambda: cmd_upload(st, cfg, client, args), "verify": lambda: cmd_verify(st, cfg, client, args),
                "remove": lambda: cmd_remove(st, cfg, client, args), "report": lambda: cmd_report(st, cfg, args)}
    code = run.run_command(st, f"takeout_upload.{args.command}", args, commands[args.command], lock=lock)
    if args.command == "remove" and not args.yes:
        run.dry_run_footer()
    return code


if __name__ == "__main__":
    sys.exit(main())
