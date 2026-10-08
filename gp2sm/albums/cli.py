"""gp2sm albums: date-sortable album names ([naming]) and album settings ([[policy]]).

Names:
  inventory   list the albums in [naming] scope; for albums with no date in their name (or only a year, or a range of years),
              sample a few pages of photos for their capture dates (--sample N checks only N random albums, --name GLOB only matching ones)
  plan        propose new names: confident ones are planned, the rest wait for review
  approve     move reviewed proposals into the plan (--name GLOB, repeatable; --as NAME picks another name)
  apply       rename (display names only; links never change); dry run unless --yes
Settings:
  audit       read the settings of every album a [[policy]] covers (--sample N, --name GLOB) and plan the fixes;
              also flags empty albums and albums near the item cap
  fix         apply the planned setting fixes (dry run unless --yes; skips settings changed since the audit)
Both:
  report      what is planned, for review, applied, and found
  verify      check that applied names and settings are still in place
  undo        restore previous names and settings (dry run unless --yes; skips anything changed since)
"""

import argparse
import fnmatch
import json
import logging
import random
import sys

from gp2sm.albums import naming, policy
from gp2sm.cli import run
from gp2sm.importer.inventory import albums_in_scope
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
    if args.name:
        albums = [a for a in albums if any(fnmatch.fnmatch((a["name"] or "").lower(), g.lower()) for g in args.name)]
    if args.sample and args.sample < len(albums):
        albums = random.Random(args.seed).sample(albums, args.sample)
    progress = run.Progress(len(albums), "albums checked")
    sampled = 0
    with st.db:
        st.db.execute("DELETE FROM albums_seen")
    for a in albums:
        info = client.album_info(a["album_id"])
        dates = None
        if nm["date_from_photos"] and naming.parse(info["name"]).precision in ("none", "year", "range"):
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
    report_settings(st, args)
    return None


def report_settings(st, args):
    rows = st.q("SELECT c.*, a.folder, a.name FROM album_changes c LEFT JOIN album_settings_seen a USING(album_id) "
                "WHERE c.kind IN ('setting', 'finding') ORDER BY a.folder, a.name, c.field")
    if not rows:
        return
    for title, kind, status in (("Setting fixes planned", "setting", "planned"),
                                ("Setting fixes for review (approve --name ALBUM)", "setting", "review"),
                                ("Settings applied", "setting", "done"),
                                ("Setting fixes failed or skipped", "setting", "failed"),
                                ("Findings and warnings", "finding", "info")):
        sel = [r for r in rows if r["kind"] == kind and (r["status"] == status or
                                                          (status == "failed" and r["status"] == "skipped"))]
        print(f"\n== {title}: {len(sel)} ==")
        for r in sel[:args.limit or 200]:
            where = f"{r['folder'] or ''}/{r['name']}"
            if kind == "finding":
                print(f"  {where}: {r['note']}")
            else:
                extra = f"  ({r['note']})" if r["note"] else ""
                print(f"  {where}: {r['field']} {json.loads(r['old_value'])!r} -> {json.loads(r['new_value'])!r}{extra}")


def cmd_audit(st, cfg, client, args):
    policies = cfg["policy"]
    if not policies:
        raise SystemExit("no [[policy]] entries in gp2sm.toml: add at least one (see the commented example there)")
    allowed = client.capabilities.album_settings
    unsupported = sorted({k for p in policies for k in p if k not in ("scope", "exclude") and k not in allowed})
    if unsupported:
        raise SystemExit(f"{client.capabilities.name} can't change: {unsupported}")
    albums = [a for a in albums_in_scope(client, ["/"])
              if any(policy.matches(a, p["scope"]) and not policy.matches(a, p.get("exclude", [])) for p in policies)]
    if args.name:
        albums = [a for a in albums if any(fnmatch.fnmatch((a["name"] or "").lower(), g.lower()) for g in args.name)]
    if args.sample and args.sample < len(albums):
        albums = random.Random(args.seed).sample(albums, args.sample)
    progress = run.Progress(len(albums), "albums audited")
    counts, stamp = {"albums": len(albums), "fixes": 0, "warnings": 0, "findings": 0}, now()
    with st.db:
        st.db.execute("DELETE FROM album_changes WHERE kind='setting' AND status IN ('planned', 'review')")
        st.db.execute("DELETE FROM album_changes WHERE kind='finding'")
        st.db.execute("DELETE FROM album_settings_seen")
    for a in albums:
        info = client.album_info(a["album_id"])
        actual = client.album_settings(a["album_id"])
        fixes, warnings = policy.drift(actual, policy.desired(a, policies))
        found = policy.findings(info, cfg["album_soft_cap"], cfg["album_hard_cap"])
        with st.db:
            st.db.execute("INSERT OR REPLACE INTO album_settings_seen VALUES(?,?,?,?,?,?)",
                          (a["album_id"], info["name"], a.get("folder"), info["item_count"], json.dumps(actual), stamp))
            for f in fixes:
                known = f["actual"] in allowed.get(f["setting"], ())
                status, note = ("planned", None) if known else (
                    "review", f"current value {f['actual']!r} isn't one gp2sm knows, so the change couldn't be undone")
                st.db.execute("INSERT INTO album_changes(album_id, kind, field, old_value, new_value, source, note, "
                              "status, planned_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (a["album_id"], "setting", f["setting"], json.dumps(f["actual"]), json.dumps(f["desired"]),
                               f"policy {f['policy']}", note, status, stamp))
            for note in warnings + found:
                st.db.execute("INSERT INTO album_changes(album_id, kind, note, status, planned_at) VALUES(?,?,?,?,?)",
                              (a["album_id"], "finding", note, "info", stamp))
        counts["fixes"] += len(fixes)
        counts["warnings"] += len(warnings)
        counts["findings"] += len(found)
        progress.update()
    progress.close()
    return counts


def cmd_fix(st, cfg, client, args):
    rows = [dict(r) for r in st.q("SELECT c.*, a.name FROM album_changes c LEFT JOIN album_settings_seen a "
                                  "USING(album_id) WHERE c.kind='setting' AND c.status='planned' ORDER BY c.change_id")]
    if args.name:
        rows = [r for r in rows if any(fnmatch.fnmatch((r["name"] or "").lower(), g.lower()) for g in args.name)]
    by_album = {}
    for r in rows:
        by_album.setdefault(r["album_id"], []).append(r)
    albums = list(by_album.items())[:args.limit] if args.limit else list(by_album.items())
    if not args.yes:
        for _, changes in albums:
            print(f"  {changes[0]['name']}: " + ", ".join(
                f"{c['field']} {json.loads(c['old_value'])!r} -> {json.loads(c['new_value'])!r}" for c in changes))
        print(f"  {sum(len(c) for _, c in albums)} settings in {len(albums)} albums")
        run.dry_run_footer()
        return None
    run.install_sigint()
    out = {"fixed": 0, "failed": 0, "skipped": 0}
    progress = run.Progress(len(albums), "albums fixed")
    for album_id, changes in albums:
        if run.Stop.requested:
            break
        current = client.album_settings(album_id)
        todo = {}
        for c in changes:
            if current.get(c["field"]) != json.loads(c["old_value"]):   # changed by someone since the audit
                st.db.execute("UPDATE album_changes SET status='skipped', last_error=? WHERE change_id=?",
                              (f"now {current.get(c['field'])!r}", c["change_id"]))
                out["skipped"] += 1
            else:
                todo[c["field"]] = json.loads(c["new_value"])
        got = client.set_album_settings(album_id, todo) if todo else current
        for c in changes:
            if c["field"] not in todo:
                continue
            ok = got.get(c["field"]) == todo[c["field"]]
            st.db.execute("UPDATE album_changes SET status=?, applied_at=?, last_error=? WHERE change_id=?",
                          ("done" if ok else "failed", now(), None if ok else f"read back {got.get(c['field'])!r}",
                           c["change_id"]))
            out["fixed" if ok else "failed"] += 1
        st.event("album_settings_fixed", album_id=album_id, commit=False, changes=todo)
        st.db.commit()
        progress.update()
    progress.close()
    return out


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
    if args.as_name:
        if len(rows) != 1:
            raise SystemExit(f"--as names exactly one reviewed rename; --name matches {len(rows)}")
        with st.db:
            st.db.execute("UPDATE album_changes SET status='planned', new_value=?, note=? WHERE change_id=?",
                          (args.as_name, (rows[0]["note"] + "; " if rows[0]["note"] else "") + "name chosen by a person",
                           rows[0]["change_id"]))
        return {"approved": 1, "new_name": args.as_name}
    rows += [dict(r) for r in st.q("SELECT c.* , a.name FROM album_changes c JOIN album_settings_seen a USING(album_id) "
                                   "WHERE c.kind='setting' AND c.status='review'")
             if any(fnmatch.fnmatch((r["name"] or "").lower(), g.lower()) for g in args.name)]
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
                st.event("album_renamed" if ok else "album_rename_failed", album_id=r["album_id"], commit=False,
                         old=r["old_value"], new=r["new_value"], read_back=got)
        except (NotFound, SmugMugError) as e:
            st.db.execute("UPDATE album_changes SET status='failed', last_error=? WHERE change_id=?",
                          (str(e)[:300], r["change_id"]))
            out["failed"] += 1
        st.db.commit()
        progress.update(failed=int(out["failed"] > 0))
    progress.close()
    return out


def newest(rows):
    """Only the newest change of each (album, setting) or album rename: gp2sm may change the same thing twice."""
    latest = {}
    for r in rows:
        key = (r["album_id"], r["kind"], r["field"] if r["kind"] == "setting" else None)
        if key not in latest or r["change_id"] > latest[key]["change_id"]:
            latest[key] = r
    return sorted(latest.values(), key=lambda r: r["change_id"])


def chain(changes):
    """changes: one album setting's (or name's) done changes, newest first. Returns (linked changes, value before the
    first of them): older changes count only while each one's new value is the next one's old value."""
    linked, before = [changes[0]], changes[0]["old_value"]
    for c in changes[1:]:
        if json.loads(c["new_value"]) != json.loads(before):
            break
        linked.append(c)
        before = c["old_value"]
    return linked, before


def cmd_verify(st, cfg, client, args):
    out = {"in_place": 0, "changed_since": []}
    settings = {}
    for r in newest(st.q("SELECT * FROM album_changes WHERE kind='setting' AND status='done'")):
        if r["album_id"] not in settings:
            try:
                settings[r["album_id"]] = client.album_settings(r["album_id"])
            except NotFound:
                settings[r["album_id"]] = {}
        value = settings[r["album_id"]].get(r["field"])
        if value == json.loads(r["new_value"]):
            out["in_place"] += 1
        else:
            out["changed_since"].append(f"{r['album_id']} {r['field']} is now {value!r}")
    for r in newest(st.q("SELECT * FROM album_changes WHERE kind='rename' AND status='done'")):
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
        settings = st.one("SELECT COUNT(*) FROM album_changes WHERE kind='setting' AND status='done'")
        print(f"  {len(rows)} renames and {settings} settings to restore")
        run.dry_run_footer()
        return None
    run.install_sigint()
    out = {"restored": 0, "skipped": [], "failed": 0}
    undo_settings(st, client, args, out)
    by_album = {}
    for r in rows:                     # newest first
        by_album.setdefault(r["album_id"], []).append(r)
    for album_id, renames in by_album.items():
        if run.Stop.requested:
            break
        current = client.album_info(album_id)["name"]
        if current != renames[0]["new_value"]:
            out["skipped"].append(f"{renames[0]['new_value']!r} was renamed since (now {current!r}); left alone")
            continue
        linked, before = chain([dict(r, new_value=json.dumps(r["new_value"]), old_value=json.dumps(r["old_value"]))
                                for r in renames])
        before = json.loads(before)
        got = client.rename_album(album_id, before)
        ok = got == before
        for r in linked:
            st.db.execute("UPDATE album_changes SET status=?, last_error=? WHERE change_id=?",
                          ("undone" if ok else "done", None if ok else f"undo read back {got!r}", r["change_id"]))
        st.event("album_rename_undone" if ok else "album_rename_undo_failed", album_id=album_id, commit=False,
                 restored=before)
        st.db.commit()
        out["restored" if ok else "failed"] += 1
    return out


def undo_settings(st, client, args, out):
    """Restore settings changed by fix, per album, unless they were changed again since."""
    rows = [dict(r) for r in st.q("SELECT c.*, a.name FROM album_changes c LEFT JOIN album_settings_seen a "
                                  "USING(album_id) WHERE c.kind='setting' AND c.status='done' ORDER BY change_id DESC")]
    if args.name:
        rows = [r for r in rows if any(fnmatch.fnmatch((r["name"] or "").lower(), g.lower()) for g in args.name)]
    by_album = {}
    for r in rows:
        by_album.setdefault(r["album_id"], []).append(r)
    for album_id, changes in by_album.items():
        if run.Stop.requested:
            break
        current = client.album_settings(album_id)
        allowed = client.capabilities.album_settings
        todo, linked_by_field = {}, {}
        by_field = {}
        for c in changes:              # newest first
            by_field.setdefault(c["field"], []).append(c)
        for field, field_changes in by_field.items():
            if current.get(field) != json.loads(field_changes[0]["new_value"]):
                out["skipped"].append(f"{field_changes[0]['name']}: {field} was changed since; left alone")
                continue
            # changed more than once by gp2sm (e.g. a test, then the real policy): back to before the first change
            linked, before = chain(field_changes)
            old = json.loads(before)
            if old not in allowed.get(field, ()):
                out["skipped"].append(f"{field_changes[0]['name']}: {field} was {old!r}, which can't be written back")
                continue
            todo[field] = old
            linked_by_field[field] = linked
        results = {}
        # download size can only be restored with downloads on: restore it before switching downloads off
        if "download_size" in todo and todo.get("downloads") is False:
            first = client.set_album_settings(album_id, {"download_size": todo["download_size"]})
            results["download_size"] = first.get("download_size") == todo.pop("download_size")
        got = client.set_album_settings(album_id, todo) if todo else current
        results.update({k: got.get(k) == v for k, v in todo.items()})
        for field, ok in results.items():
            for c in linked_by_field[field]:
                st.db.execute("UPDATE album_changes SET status=? WHERE change_id=?", ("undone" if ok else "done",
                                                                                     c["change_id"]))
            out["restored" if ok else "failed"] += 1
        st.db.commit()


COMMANDS = {"inventory": cmd_inventory, "plan": cmd_plan, "report": cmd_report, "approve": cmd_approve,
            "apply": cmd_apply, "audit": cmd_audit, "fix": cmd_fix, "verify": cmd_verify, "undo": cmd_undo}
NEEDS_CLIENT = {"inventory", "apply", "audit", "fix", "verify", "undo"}
WRITES = {"apply", "fix", "undo"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm albums", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    context.add_args(p)
    p.add_argument("--sample", type=int, help="inventory/audit: check only this many random albums")
    p.add_argument("--seed", type=int, default=1, help="inventory/audit: which random sample (repeatable)")
    p.add_argument("--name", action="append", help="only albums whose name (old or new) matches this glob")
    p.add_argument("--as", dest="as_name", help="approve: rename the one matching album to this name instead")
    p.add_argument("--limit", type=int, help="apply/fix: at most this many albums; report: rows shown per section")
    run.add_yes(p, "rename / fix / undo")
    args = p.parse_args(argv)
    cfg = context.resolve(args)
    run.setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = context.client(cfg) if args.command in NEEDS_CLIENT else None
    lock = None
    if args.command in WRITES and args.yes:
        lock = context.acquire_lock(cfg["state_db"], f"albums {args.command}")
    command = COMMANDS[args.command]
    return run.run_command(st, f"albums.{args.command}", args, lambda: command(st, cfg, client, args), lock=lock)


if __name__ == "__main__":
    sys.exit(main())
