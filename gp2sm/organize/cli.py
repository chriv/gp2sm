"""gp2sm organize: gather items already on the destination into dated (and grouped) albums, by rules.

  inventory   list the [organize] sources (folders or albums by name; albums inside the project's own
              folder are never sources) with each item's camera date, make and model
  plan        date every item (the [organize] dates chain), group it ([[organize.group]]), pick its album
  report      what the plan does, per album
  apply       move the items (dry run unless --yes); verify | reconcile | undo as in `gp2sm consolidate`
  delete-duplicates | delete-empty-sources | delete-empty-targets   permanent, dry run unless --yes

Re-running is safe: items already moved keep their album, and new items are added to the plan.
"""

import argparse
import logging
import sys

from gp2sm.cli import run
from gp2sm.contrib.legacy_bridge import legacy_dates
from gp2sm.importer.inventory import albums_in_scope
from gp2sm.organize import consolidate, planning, rules
from gp2sm.project import context
from gp2sm.state import State

log = logging.getLogger("gp2sm.organize.cli")


def inside(folder, project_folder):
    folder, project_folder = (folder or "").strip("/"), project_folder.strip("/")
    return folder == project_folder or folder.startswith(project_folder + "/")


def cmd_inventory(st, cfg, client, args):
    sources = cfg["organize"]["sources"]
    if not sources:
        raise SystemExit("[organize] sources is empty: name the folders or albums to organize")
    albums = [a for a in albums_in_scope(client, sources) if not inside(a["folder"], cfg["target_folder"])]
    log.info("%d source albums under %s", len(albums), sources)
    return consolidate.inventory_albums(st, client, albums, args.workers)


def plugins(st):
    """Extra date sources: 'legacy' reads dates linked from the old transfer databases (gp2sm consolidate match)."""
    return {"legacy": legacy_dates.plugin(st)}


def organize_items(st, cfg):
    """Items with their date, method and group (pure rules applied to the inventory)."""
    org = cfg["organize"]
    extra = plugins(st)
    out, skipped = [], []
    for r in st.q("SELECT i.image_key AS item_id, i.filename, i.is_video, i.archived_md5 AS md5, i.uploaded, "
                  "i.capture_dt_smug AS capture_time, i.make, i.model, a.name AS album_name, a.folder "
                  "FROM images i LEFT JOIN source_albums a ON a.album_key = i.src_album_key"):
        it = dict(r)
        if rules.recent(it["uploaded"], org["skip_newer_than_days"]):
            skipped.append(it["item_id"])
            continue
        it["capture_local"], it["method"] = rules.date_for(it, org["dates"], cfg["timezone"], plugins=extra)
        album = "/".join(p for p in (it["folder"], it["album_name"]) if p)
        it["group"] = rules.group_for({"album": album, "make": it["make"], "model": it["model"],
                                       "filename": it["filename"]}, org["group"], org["unassigned"])
        out.append(it)
    return out, skipped


def cmd_plan(st, cfg, client, args):
    items, skipped = organize_items(st, cfg)
    existing = {r["item_id"]: dict(r) for r in st.q("SELECT image_key AS item_id, status, target_name FROM plan")}
    org = cfg["organize"]
    settings = {"photo": cfg["photo_album_template"], "video": cfg["video_album_template"],
                "undated_photo": cfg["undated_photo_album"], "undated_video": cfg["undated_video_album"],
                "duplicates_album": cfg["duplicates_album"], "duplicates": org["duplicates"],
                "soft_cap": cfg["album_soft_cap"]}
    rows = planning.plan_organize(items, settings, existing)
    with st.db:   # recent items wait for a later run; only plan rows that haven't started are withdrawn
        st.db.executemany("DELETE FROM plan WHERE image_key=? AND status IN ('pending', 'failed')",
                          [(k,) for k in skipped])
    out = consolidate.write_plan(st, rows, existing)
    methods = {}
    for it in items:
        key = it["method"].split(":")[0]
        methods[key] = methods.get(key, 0) + 1
    out.update(dated_by=dict(sorted(methods.items())), skipped_recent=len(skipped),
               groups=dict(sorted({g: sum(1 for i in items if i["group"] == g) for g in {i["group"] for i in items}}
                                  .items())))
    return out


def cmd_apply(st, cfg, client, args):
    if cfg["organize"]["mode"] == "collect":
        raise SystemExit("[organize] mode = \"collect\" isn't available yet (roadmap A4.3); use mode = \"move\"")
    return consolidate.cmd_apply(st, cfg, client, args)


COMMANDS = {"inventory": cmd_inventory, "plan": cmd_plan, "report": consolidate.cmd_report, "apply": cmd_apply,
            "reconcile": consolidate.cmd_reconcile, "verify": consolidate.cmd_verify, "undo": consolidate.cmd_undo,
            "delete-duplicates": consolidate.cmd_delete_duplicates,
            "delete-empty-sources": consolidate.cmd_delete_empty_sources,
            "delete-empty-targets": consolidate.cmd_delete_empty_targets}
NEEDS_CLIENT = set(COMMANDS) - {"plan", "report"}
WRITES = {"apply", "undo", "delete-duplicates", "delete-empty-sources", "delete-empty-targets"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm organize", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    context.add_args(p)
    p.add_argument("--debug", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("inventory")
    s.add_argument("--workers", type=int, default=4)
    sub.add_parser("plan")
    s = sub.add_parser("report")
    s.add_argument("--targets", action="store_true", help="list every target album")
    s = sub.add_parser("apply")
    run.add_yes(s, "move the items")
    s.add_argument("--limit", type=int, help="max items to move this run")
    s.add_argument("--target", action="append", help="only these target album names (glob ok; repeatable)")
    s.add_argument("--kind", action="append", choices=list(consolidate.KIND_ORDER), help="only these target kinds")
    s.add_argument("--batch-size", type=int)
    s = sub.add_parser("reconcile")
    s.add_argument("--include-failed", action="store_true")
    sub.add_parser("verify")
    for name in ("delete-duplicates", "delete-empty-sources", "delete-empty-targets"):
        s = sub.add_parser(name)
        run.add_yes(s, "delete (permanent)")
        if name == "delete-empty-targets":
            s.add_argument("--name", action="append", help="only target albums matching this glob (repeatable)")
    s = sub.add_parser("undo")
    s.add_argument("target", help="exact target album name to move back out")
    run.add_yes(s, "move the items back")
    args = p.parse_args(argv)

    cfg = context.resolve(args)
    consolidate.setup_logging(cfg["log_file"], args.debug)
    st = State(cfg["state_db"])
    client = context.client(cfg) if args.command in NEEDS_CLIENT else None
    lock = None
    if args.command in WRITES and args.yes:
        lock = context.acquire_lock(cfg["state_db"], f"organize {args.command}")
    command = COMMANDS[args.command]
    code = run.run_command(st, f"organize.{args.command}", args, lambda: command(st, cfg, client, args), lock=lock)
    if args.command.startswith("delete-") and not args.yes:
        run.dry_run_footer()
    return code


if __name__ == "__main__":
    sys.exit(main())
