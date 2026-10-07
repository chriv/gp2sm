"""gp2sm report: the project's report: what happened to every source file, the evidence behind each decision,
verification status and activity, written to the project's reports/ folder as Markdown, HTML, JSON and a
per-file CSV. Reports name real albums and files, so they stay in the (private) project folder."""

import argparse
import json
import os
import sys

from gp2sm.cli import run
from gp2sm.project import context
from gp2sm.report import model, render
from gp2sm.state import State
from gp2sm.takeout.source import TakeoutSource

REJECTED_BY_DEFAULT = (".webp", ".bmp", ".ico")


def takeout_sources(args, cfg, rejected):
    """Classification input for every media file in the Takeout index (None if there's no index)."""
    if not args.index or not os.path.exists(args.index):
        return None
    pol = cfg["takeout"]
    out = []
    for i in TakeoutSource(args.index, args.takeout_dir).iter_items(include_orphans=True):
        ext = os.path.splitext(i.name)[1].lower()
        skip = "rejected_types" if i.kind == "photo" and ext in rejected and pol["rejected_types"] == "skip" else None
        out.append({"ref": i.source_id, "kind": i.kind, "policy_skip": skip})
        if i.motion_ref:
            clip_skip = "live_clips" if pol["live_clips"] == "skip" else \
                "unpaired_clips" if pol["unpaired_clips"] == "skip" else None
            out.append({"ref": i.motion_ref, "kind": "clip", "still_ref": i.source_id, "policy_skip": clip_skip})
    return out


def last_plan_notes(st):
    row = st.one("SELECT summary FROM runs WHERE command='takeout.plan' AND status='ok' ORDER BY run_id DESC LIMIT 1")
    return (json.loads(row) or {}).get("not planned", {}) if row else {}


def cmd_report(st, cfg, client, args):
    rejected = client.capabilities.rejected_extensions if client else REJECTED_BY_DEFAULT
    report = model.build(st, cfg, takeout_sources(args, cfg, rejected), last_plan_notes(st))
    folder = os.path.join(args.project_root, "reports")
    os.makedirs(folder, exist_ok=True)
    paths = {}
    for ext, fn in (("md", render.to_markdown), ("html", render.to_html), ("json", render.to_json)):
        paths[ext] = os.path.join(folder, f"report.{ext}")
        with open(paths[ext], "w") as f:
            f.write(fn(report))
    if report.get("per_file"):
        paths["csv"] = os.path.join(folder, "files.csv")
        with open(paths["csv"], "w", newline="") as f:
            f.write(render.to_csv(report))
    acc = report.get("accounting")
    summary = {"status": "in progress" if report["in_progress"] else "final", "written": paths}
    if acc:
        summary["files"] = acc["totals"]["total"]
        summary["outcomes"] = {o: acc["totals"][o] for o in acc["outcomes"]}
        summary["every file counted once"] = acc["ok"]
        if not acc["ok"]:
            raise SystemExit(f"the report's counts don't add up (see {paths['md']}); please report this")
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm report", description=__doc__)
    context.add_args(p, paths=("index", "takeout_dir"))
    p.add_argument("--offline", action="store_true", help="don't contact the destination (uses default rules)")
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    run.setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = None if args.offline or not cfg.get("smugmug_config") else context.client(cfg)
    return run.run_command(st, "report", args, lambda: cmd_report(st, cfg, client, args))


if __name__ == "__main__":
    sys.exit(main())
