"""gp2sm albums: consistent, date-sortable album names (A6); settings policy follows in A7.

  inventory   list the albums in [naming] scope; for albums with no date in their name, sample a few pages
              of photos for their capture dates (--sample N checks only N random albums)
  plan        propose new names: confident ones are planned, the rest wait for review
  report      the planned renames, the ones for review, and why the others are left alone
  approve     move reviewed proposals into the plan (--name GLOB, repeatable)
  apply       rename (display names only; links never change); dry run unless --yes
  verify      check that applied names are still in place
  undo        restore the previous names (dry run unless --yes; skips albums renamed since)
"""

import argparse
import fnmatch
import json
import logging
import random
import sys

from gp2sm.albums import naming
from gp2sm.cli import run
from gp2sm.importer.inventory import albums_in_scope
from gp2sm.organize.consolidate import setup_logging
from gp2sm.project import context
from gp2sm.smugmug.client import NotFound, SmugMugError
from gp2sm.state import State, now

log = logging.getLogger("gp2sm.albums.cli")

PAGE = 20


def excluded(album, patterns):
    path = "/".join(p for p in (album.get("folder"), album.get("name")) if p)
    return any(fnmatch.fnmatch((album.get("name") or "").lower(), p.lower()) or fnmatch.fnmatch(path.lower(), p.lower())
               for p in patterns)


def page_starts(item_count, wanted, seed):
    """1-based page starts spread over the whole album: one random page from each of k equal slices
    (purely random pages can bunch together and miss most of a large album). Repeatable per seed."""
    total_pages = -(-item_count // PAGE)
    k = max(1, min(-(-wanted // PAGE), total_pages))
    rng = random.Random(seed)
    starts = []
    for i in range(k):
        lo, hi = i * total_pages // k, max(i * total_pages // k, (i + 1) * total_pages // k - 1)
        starts.append(rng.randint(lo, hi) * PAGE + 1)
    return starts


def sample_dates(client, album_id, item_count, wanted, seed):
    """{'captured': [...], 'uploaded': [...]} from pages spread over the album (repeatable for the same album)."""
    out = {"captured": [], "uploaded": []}
    if not item_count:
        return out
    for start in page_starts(item_count, wanted, seed):
        for it in client.album_items_page(album_id, start, PAGE, with_metadata=True):
            if it.get("capture_time"):
                out["captured"].append(it["capture_time"])
            if it.get("uploaded"):
                out["uploaded"].append(it["uploaded"])
    return out


def cmd_inventory(st, cfg, client, args):
    nm = cfg["naming"]
    if not nm["scope"]:
        raise SystemExit("[naming] scope is empty: name the folders or albums whose names to check ([\"/\"] = all)")
    albums = [a for a in albums_in_scope(client, nm["scope"]) if not excluded(a, nm["exclude"])]
    if args.sample and args.sample < len(albums):
        albums = random.Random(args.seed).sample(albums, args.sample)
    progress = run.Progress(len(albums), "albums checked")
    sampled = 0
    with st.db:
        st.db.execute("DELETE FROM albums_seen")
    for a in albums:
        info = client.album_info(a["album_id"])
        dates = None
        if nm["date_from_photos"] and naming.parse(info["name"]).precision in ("none", "year"):
            dates = sample_dates(client, a["album_id"], info["item_count"], nm["photo_sample"], a["album_id"])
            sampled += 1
        dates = dates or {"captured": [], "uploaded": []}
        with st.db:
            st.db.execute("INSERT OR REPLACE INTO albums_seen VALUES(?,?,?,?,?,?)",
                          (a["album_id"], info["name"], a.get("folder"), info["item_count"], json.dumps(dates), now()))
        progress.update()
    progress.close()
    return {"albums": len(albums), "photo_samples": sampled}


def cmd_plan(st, cfg, client, args):
    nm = cfg["naming"]
    templates = {"month": nm["month"], "year": nm["year"], "day": nm["day"]}
    rows = [dict(r) for r in st.q("SELECT * FROM albums_seen ORDER BY folder, name")]
    names_by_folder = {}
    for r in rows:
        names_by_folder.setdefault(r["folder"], set()).add((r["name"] or "").lower())
    counts, stamp = {}, now()
    with st.db:
        st.db.execute("DELETE FROM album_changes WHERE kind='rename' AND status IN ('planned', 'review')")
        for r in rows:
            if st.one("SELECT 1 FROM album_changes WHERE album_id=? AND kind='rename' AND status='done' "
                      "AND new_value=?", r["album_id"], r["name"]):
                counts["already renamed by gp2sm"] = counts.get("already renamed by gp2sm", 0) + 1
                continue
            sample = json.loads(r["photo_dates"] or "{}") or {}
            prop = naming.propose(r["name"], templates, nm["keep_day"], nm["min_confidence"],
                                  sample.get("captured") or [], nm["max_spread_days"],
                                  min_photos=nm["min_photos"], upload_dates=sample.get("uploaded"))
            if not prop.new:
                counts[prop.note.split(";")[0]] = counts.get(prop.note.split(";")[0], 0) + 1
                continue
            status, note = ("planned" if prop.auto else "review"), prop.note
            taken = names_by_folder.get(r["folder"], set()) - {(r["name"] or "").lower()}
            if prop.new.lower() in taken:
                status, note = "review", f"another album in this folder is already called {prop.new!r}"
            names_by_folder.setdefault(r["folder"], set()).add(prop.new.lower())
            st.db.execute("INSERT INTO album_changes(album_id, kind, field, old_value, new_value, confidence, source, "
                          "note, status, planned_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (r["album_id"], "rename", "name", r["name"], prop.new, prop.confidence, prop.source, note,
                           status, stamp))
            counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def cmd_report(st, cfg, client, args):
    for title, status in (("Renames planned (confident)", "planned"), ("For review (gp2sm albums approve --name ...)", "review"),
                          ("Applied", "done"), ("Failed", "failed")):
        rows = st.q("SELECT c.old_value, c.new_value, c.source, c.confidence, c.note, a.folder FROM album_changes c "
                    "LEFT JOIN albums_seen a USING(album_id) WHERE c.kind='rename' AND c.status=? "
                    "ORDER BY a.folder, c.old_value", status)
        print(f"\n== {title}: {len(rows)} ==")
        for r in rows[:args.limit or 200]:
            extra = f"  [{r['source']}, {r['confidence']}{'; ' + r['note'] if r['note'] else ''}]"
            print(f"  {r['folder'] or ''}/{r['old_value']}  ->  {r['new_value']}{extra}")
    return None


def matching(rows, globs):
    if not globs:
        return rows
    return [r for r in rows if any(fnmatch.fnmatch((r["old_value"] or "").lower(), g.lower()) or
                                   fnmatch.fnmatch((r["new_value"] or "").lower(), g.lower()) for g in globs)]


def cmd_approve(st, cfg, client, args):
    if not args.name:
        raise SystemExit("name the reviewed albums to approve with --name GLOB (old or new name)")
    rows = matching([dict(r) for r in st.q("SELECT * FROM album_changes WHERE kind='rename' AND status='review'")],
                    args.name)
    with st.db:
        for r in rows:
            st.db.execute("UPDATE album_changes SET status='planned', note=? WHERE change_id=?",
                          ((r["note"] + "; " if r["note"] else "") + "approved by a person", r["change_id"]))
    return {"approved": len(rows)}


def cmd_apply(st, cfg, client, args):
    rows = matching([dict(r) for r in st.q("SELECT * FROM album_changes WHERE kind='rename' AND status='planned' "
                                           "ORDER BY change_id")], args.name)
    if args.limit:
        rows = rows[:args.limit]
    if not args.yes:
        for r in rows:
            print(f"  would rename {r['old_value']!r} -> {r['new_value']!r}")
        print(f"  {len(rows)} albums (display names only; links don't change)")
        run.dry_run_footer()
        return None
    run.install_sigint()
    out = {"renamed": 0, "failed": 0, "skipped": 0}
    progress = run.Progress(len(rows), "renaming")
    for r in rows:
        if run.Stop.requested:
            break
        try:
            current = client.album_info(r["album_id"])["name"]
            if current != r["old_value"]:   # renamed by someone else since the plan: leave it
                st.db.execute("UPDATE album_changes SET status='skipped', last_error=? WHERE change_id=?",
                              (f"name is now {current!r}", r["change_id"]))
                out["skipped"] += 1
            else:
                got = client.rename_album(r["album_id"], r["new_value"])
                ok = got == r["new_value"]
                st.db.execute("UPDATE album_changes SET status=?, applied_at=?, last_error=? WHERE change_id=?",
                              ("done" if ok else "failed", now(), None if ok else f"read back {got!r}", r["change_id"]))
                out["renamed" if ok else "failed"] += 1
                st.event("album_renamed" if ok else "album_rename_failed", album_key=r["album_id"], commit=False,
                         old=r["old_value"], new=r["new_value"], read_back=got)
        except (NotFound, SmugMugError) as e:
            st.db.execute("UPDATE album_changes SET status='failed', last_error=? WHERE change_id=?",
                          (str(e)[:300], r["change_id"]))
            out["failed"] += 1
        st.db.commit()
        progress.update(failed=int(out["failed"] > 0))
    progress.close()
    return out


def cmd_verify(st, cfg, client, args):
    out = {"in_place": 0, "changed_since": []}
    for r in st.q("SELECT * FROM album_changes WHERE kind='rename' AND status='done'"):
        try:
            name = client.album_info(r["album_id"])["name"]
        except NotFound:
            name = None
        if name == r["new_value"]:
            out["in_place"] += 1
        else:
            out["changed_since"].append(f"{r['new_value']!r} is now {name!r}")
    return out


def cmd_undo(st, cfg, client, args):
    rows = matching([dict(r) for r in st.q("SELECT * FROM album_changes WHERE kind='rename' AND status='done' "
                                           "ORDER BY change_id DESC")], args.name)
    if not args.yes:
        for r in rows:
            print(f"  would rename {r['new_value']!r} back to {r['old_value']!r}")
        print(f"  {len(rows)} albums")
        run.dry_run_footer()
        return None
    run.install_sigint()
    out = {"restored": 0, "skipped": [], "failed": 0}
    for r in rows:
        if run.Stop.requested:
            break
        current = client.album_info(r["album_id"])["name"]
        if current != r["new_value"]:
            out["skipped"].append(f"{r['new_value']!r} was renamed since (now {current!r}); left alone")
            continue
        got = client.rename_album(r["album_id"], r["old_value"])
        ok = got == r["old_value"]
        st.db.execute("UPDATE album_changes SET status=?, last_error=? WHERE change_id=?",
                      ("undone" if ok else "done", None if ok else f"undo read back {got!r}", r["change_id"]))
        st.event("album_rename_undone" if ok else "album_rename_undo_failed", album_key=r["album_id"], commit=False,
                 restored=r["old_value"])
        st.db.commit()
        out["restored" if ok else "failed"] += 1
    return out


COMMANDS = {"inventory": cmd_inventory, "plan": cmd_plan, "report": cmd_report, "approve": cmd_approve,
            "apply": cmd_apply, "verify": cmd_verify, "undo": cmd_undo}
NEEDS_CLIENT = {"inventory", "apply", "verify", "undo"}
WRITES = {"apply", "undo"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm albums", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    context.add_args(p)
    p.add_argument("--sample", type=int, help="inventory: check only this many random albums")
    p.add_argument("--seed", type=int, default=1, help="inventory: which random sample (repeatable)")
    p.add_argument("--name", action="append", help="approve/apply/undo: only albums whose old or new name matches")
    p.add_argument("--limit", type=int, help="apply: at most this many; report: rows shown per section")
    run.add_yes(p, "rename / undo")
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = context.client(cfg) if args.command in NEEDS_CLIENT else None
    lock = None
    if args.command in WRITES and args.yes:
        lock = context.acquire_lock(cfg["state_db"], f"albums {args.command}")
    command = COMMANDS[args.command]
    return run.run_command(st, f"albums.{args.command}", args, lambda: command(st, cfg, client, args), lock=lock)


if __name__ == "__main__":
    sys.exit(main())
