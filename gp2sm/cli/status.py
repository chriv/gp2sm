"""gp2sm status: where settings come from, whether credentials are in place, and what the state database holds."""

import argparse
import os
import sqlite3

from gp2sm.project import context, credentials

STATUS_TABLES = ("plan", "uploads", "placements", "datings")


def _lock_holder(state_db):
    path = os.path.join(os.path.dirname(os.path.abspath(state_db)), ".gp2sm.lock")
    try:
        pid, label = open(path).read().split(" ", 1)
    except (OSError, ValueError):
        return None
    alive = context._alive(int(pid)) if pid.isdigit() else False
    return f"{label.strip()} (pid {pid}{'' if alive else ', stale'})"


def summarize(state_db):
    """{table: {status: count}}, schema version, last runs. Read-only; never creates the database."""
    if not os.path.exists(state_db):
        return None
    db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {"tables": {}, "schema": None, "runs": []}
    if "schema_history" in tables:
        out["schema"] = db.execute("SELECT MAX(version) FROM schema_history").fetchone()[0]
    for t in STATUS_TABLES:
        if t in tables:
            out["tables"][t] = dict(db.execute(f"SELECT status, COUNT(*) FROM {t} GROUP BY status ORDER BY status"))
    if "runs" in tables:
        out["runs"] = db.execute("SELECT command, started, status FROM runs ORDER BY run_id DESC LIMIT 5").fetchall()
    db.close()
    return out


def main(argv=None, show=print):
    p = argparse.ArgumentParser(prog="gp2sm status", description=__doc__)
    context.add_args(p, paths=("index",))
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    if args.project_root:
        show(f"project:      {args.project_root}")
        name = cfg["_credentials_name"]
        show(f"credentials:  {name} ({'stored' if credentials.exists(name) else 'MISSING: run `gp2sm auth smugmug --name ' + name + '`'})")
    else:
        show(f"settings:     legacy JSON ({args.config or context.LEGACY_CONFIG})")
        show(f"credentials:  {cfg['smugmug_config']}")
    show(f"destination:  folder {cfg['target_folder']!r}")
    show(f"state:        {cfg['state_db']}")
    show(f"index:        {args.index}{'' if os.path.exists(args.index) else ' (not built yet)'}")
    holder = _lock_holder(cfg["state_db"])
    show(f"lock:         {holder or 'free'}")
    s = summarize(cfg["state_db"])
    if s is None:
        show("\nNo state yet: nothing has been run in this project.")
        return 0
    show(f"\nschema version {s['schema']}")
    for table, counts in s["tables"].items():
        if counts:
            show(f"  {table:<11}" + ", ".join(f"{k or '-'} {v}" for k, v in counts.items()))
    if s["runs"]:
        show("recent runs:")
        for command, started, status in s["runs"]:
            show(f"  {started}  {command:<22} {status or 'running/interrupted'}")
    return 0
