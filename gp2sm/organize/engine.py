"""The organize engine: inventory storage, plan writing, and the steps that change the destination.

Used by `gp2sm organize` (organize/cli.py), and by the Takeout importer for its target albums:

  inventory_albums     record albums and their items (with metadata) in source_albums / items
  write_plan           store plan rows; work that has started is never re-planned
  cmd_apply            create albums and move or collect items (batched, verified, resumable)
  cmd_reconcile        re-check in-progress/unknown items against the server
  cmd_verify           compare every target/source album on the server with the plan
  cmd_undo             move a target album's items back (or remove collected copies)
  cmd_delete_*         gated deletions: duplicates, empty sources, empty albums this tool created
"""

import concurrent.futures
import fnmatch
import json
import logging
import uuid

from gp2sm.cli import run
from gp2sm.smugmug.client import NotFound, SmugMugError
from gp2sm.state import now

log = logging.getLogger("gp2sm.organize.engine")

KIND_ORDER = {"photo": 0, "video": 1, "photo_undated": 2, "video_undated": 3, "duplicates": 4}


# ---------------------------------------------------------------- inventory

def inventory_albums(st, client, albums, workers):
    """Record the albums and every item in them (with metadata) in source_albums/items."""
    for a in albums:
        st.db.execute("INSERT INTO source_albums(album_id, album_ref, name, path, item_count, folder) "
                      "VALUES(?,?,?,?,?,?) ON CONFLICT(album_id) DO UPDATE SET name=excluded.name, "
                      "path=excluded.path, item_count=excluded.item_count, "
                      "folder=COALESCE(excluded.folder, source_albums.folder)",
                      (a["album_id"], a["ref"], a["name"], a["path"], a["item_count"], a.get("folder")))
    st.db.commit()

    def fetch(album):
        return album, list(client.list_album_items(album["album_id"], with_metadata=True))

    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inv") as pool:
        # albums listed by folder don't report a count up front, so None means "list it"
        for album, items in pool.map(fetch, [a for a in albums if a["item_count"] is None or a["item_count"]]):
            key = album["album_id"]
            ts = now()
            with st.db:
                for it in items:
                    st.db.execute(
                        "INSERT INTO items(item_id, serial, src_album_id, src_item_ref, current_album_id,"
                        " name, format, is_video, md5, size, width, height, duration_s,"
                        " uploaded, capture_time, raw, raw_metadata, first_seen, last_seen, make, model)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(item_id) DO UPDATE SET current_album_id=excluded.current_album_id,"
                        " md5=excluded.md5, raw=excluded.raw,"
                        " raw_metadata=excluded.raw_metadata, last_seen=excluded.last_seen,"
                        " capture_time=excluded.capture_time, make=excluded.make, model=excluded.model",
                        (it["item_id"], it["serial"], key, it["item_ref"], key, it["name"], it["format"],
                         int(it["is_video"]), it["md5"], it["size"], it["width"], it["height"], it["duration_s"],
                         it["uploaded"], it["capture_time"], json.dumps(it["raw"]),
                         json.dumps(it["raw_metadata"]) if it.get("raw_metadata") else None, ts, ts,
                         it.get("make"), it.get("model")))
                st.db.execute("UPDATE source_albums SET rows_stored=?, item_count=COALESCE(item_count, ?), "
                              "inventoried_at=? WHERE album_id=?", (len(items), len(items), ts, key))
                st.event("inventory_album", album_id=key, commit=False, rows=len(items),
                         item_count=album["item_count"], path=album["path"])
            flag = "" if album["item_count"] in (None, len(items)) else f" (album reports {album['item_count']})"
            log.info("inventoried %s %s: %d%s", key, album["path"], len(items), flag)
            total += len(items)
    return {"albums": len(albums), "items": total}


# --------------------------------------------------------------------- plan

def write_plan(st, rows, existing):
    """Store plan rows. Work that has started (done/in_progress/unknown) is never re-planned."""
    ts = now()
    changed = 0
    with st.db:
        for r in rows:
            prior = existing.get(r["item_id"])
            if prior and prior["status"] in ("done", "in_progress", "unknown"):
                continue  # never re-plan work that has started
            if prior and prior["target_name"] == r["target_name"] and prior["status"] in ("pending", "failed"):
                st.db.execute("UPDATE plan SET action=?, reason=?, keeper_item_id=? WHERE item_id=?",
                              (r["action"], r["reason"], r.get("keeper_item_id"), r["item_id"]))
                continue
            st.db.execute("INSERT INTO plan(item_id, action, target_name, reason, keeper_item_id, status, planned_at,"
                          " updated_at) VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(item_id) DO UPDATE SET"
                          " action=excluded.action, target_name=excluded.target_name, reason=excluded.reason,"
                          " keeper_item_id=excluded.keeper_item_id, status='pending', updated_at=excluded.updated_at",
                          (r["item_id"], r["action"], r["target_name"], r["reason"], r.get("keeper_item_id"), ts, ts))
            changed += 1
        kinds = {r["target_name"]: r["kind"] for r in rows}
        for name, planned in st.q("SELECT target_name, COUNT(*) FROM plan GROUP BY target_name"):
            st.db.execute("INSERT INTO targets(name, kind, planned) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET"
                          " planned=excluded.planned, kind=COALESCE(excluded.kind, targets.kind)",
                          (name, kinds.get(name), planned))
        st.db.execute("DELETE FROM targets WHERE album_id IS NULL AND name NOT IN (SELECT target_name FROM plan)")
        st.event("plan", commit=False, rows=len(rows), changed=changed)
    return {"rows": len(rows), "changed": changed,
            "targets": st.one("SELECT COUNT(*) FROM targets")}


# ------------------------------------------------------------------- report

def cmd_report(st, cfg, client, args):
    def table(title, sql):
        rows = st.q(sql)
        print(f"\n== {title} ==")
        if not rows:
            print("  (none)")
            return
        cols = rows[0].keys()
        widths = [max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols]
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths, strict=True)))
        for r in rows:
            print("  " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths, strict=True)))

    table("Source albums", "SELECT path, item_count, rows_stored FROM source_albums ORDER BY path")
    table("Plan status", "SELECT action, status, COUNT(*) n FROM plan GROUP BY 1,2 ORDER BY 1,2")
    if args.targets:
        table("Targets", "SELECT t.name, t.kind, t.album_id, t.planned, "
                         "SUM(p.status='done') done, SUM(p.status='failed') failed, t.server_count "
                         "FROM targets t LEFT JOIN plan p ON p.target_name=t.name GROUP BY t.name ORDER BY t.name")
    else:
        table("Targets by kind", "SELECT kind, COUNT(*) albums, SUM(planned) items, MIN(name) first, MAX(name) last "
                                 "FROM targets GROUP BY kind")
    table("Recent failures", "SELECT item_id, target_name, attempts, substr(last_error,1,90) err FROM plan "
                             "WHERE status='failed' ORDER BY updated_at DESC LIMIT 10")
    return None


# -------------------------------------------------------------------- apply

def ensure_target(st, cfg, client, folder_node_uri, name):
    row = st.q("SELECT * FROM targets WHERE name=?", name)[0]
    if row["album_id"]:
        return row["album_id"]
    # "2016/2016-08": the album 2016-08 in the subfolder 2016 of the destination folder
    sub, _, leaf = name.rpartition("/")
    parent = (client.ensure_folder_path(client.root_folder(), f"{cfg['target_folder']}/{sub}") if sub
              else folder_node_uri)
    album_id, album_ref, node_ref, created = client.ensure_album(parent, leaf)
    st.db.execute("UPDATE targets SET album_id=?, album_ref=?, node_ref=?, created_at=? WHERE name=?",
                  (album_id, album_ref, node_ref, now() if created else None, name))
    st.event("album_created" if created else "album_found", album_id=album_id, name=name)
    log.info("%s album %r -> %s", "created" if created else "found existing", name, album_id)
    return album_id


def reconcile_keys(st, client, keys):
    """Ask the server where each image is now and fix plan/items accordingly."""
    fixed = {"done": 0, "pending": 0, "failed": 0}
    for key in keys:
        row = st.q("SELECT p.target_name, p.action, t.album_id AS target_key, i.serial, i.src_album_id, "
                   "i.current_album_id "
                   "FROM plan p JOIN items i USING(item_id) LEFT JOIN targets t ON t.name=p.target_name "
                   "WHERE p.item_id=?", key)[0]
        try:
            where = client.item_album_ids(key, row["serial"])
        except NotFound:
            where = []
        if row["target_key"] and row["target_key"] in where:
            # a collected item stays where it was too; refs keep pointing at that copy
            status = "done"
            current = row["current_album_id"] if row["action"] == "collect" else row["target_key"]
        elif where:
            status, current = "pending", where[0]
        else:
            status, current = "failed", None
        st.db.execute("UPDATE plan SET status=?, last_error=?, updated_at=? WHERE item_id=?",
                      (status, None if status != "failed" else "image not found in any album", now(), key))
        if current:
            st.db.execute("UPDATE items SET current_album_id=? WHERE item_id=?", (current, key))
        st.event("reconciled", item_id=key, album_id=current, commit=False, status=status, albums=where)
        fixed[status] += 1
    st.db.commit()
    return fixed


def move_batch(st, client, target_key, items, batch_id, collect=False):
    """Move (or collect) items (list of plan/image rows) into target. Returns (done_keys, failed_keys)."""
    keys = [r["item_id"] for r in items]
    transfer = client.collect_items if collect else client.move_items
    st.set_plan_status(keys, "in_progress", batch_id=batch_id, bump_attempts=True)
    st.db.commit()
    refs = [client.item_ref(r["current_album_id"], r["item_id"], r["serial"]) for r in items]
    before = client.album_item_count(target_key)
    try:
        transfer(target_key, refs)
    except SmugMugError as e:
        if e.http_status == 400 and not e.ambiguous:
            # Batch moves are all-or-nothing: nothing moved. Isolate the bad item(s).
            st.set_plan_status(keys, "pending")
            st.event("batch_rejected", level="warning", album_id=target_key, commit=False,
                     batch_id=batch_id, size=len(items), error=str(e))
            if len(items) == 1:
                # Confirm with the server before calling it failed (it may already be in the target).
                reconcile_keys(st, client, keys)
                if st.one("SELECT status FROM plan WHERE item_id=?", keys[0]) == "done":
                    return keys, []
                st.set_plan_status(keys, "failed", error=str(e)[:500])
                st.db.commit()
                return [], keys
            done, failed = [], []
            for it in items:
                d, f = move_batch(st, client, target_key, [it], f"{batch_id}.{it['item_id']}", collect)
                done += d
                failed += f
            return done, failed
        # Unknown outcome (network/5xx after retries): ask the server.
        st.set_plan_status(keys, "unknown", error=str(e)[:500])
        st.event("batch_unknown", level="error", album_id=target_key, commit=False, batch_id=batch_id, error=str(e))
        st.db.commit()
        reconcile_keys(st, client, keys)
        status = {k: st.one("SELECT status FROM plan WHERE item_id=?", k) for k in keys}
        # 'pending' ones (still in source) are simply retried on a later run
        return [k for k in keys if status[k] == "done"], [k for k in keys if status[k] == "failed"]

    after = client.album_item_count(target_key)
    if after - before == len(items):
        done = keys
    else:
        st.event("count_mismatch", level="warning", album_id=target_key, commit=False,
                 batch_id=batch_id, before=before, after=after, size=len(items))
        done = [r["item_id"] for r in items if client.album_contains(target_key, r["item_id"], r["serial"])]
    failed = [k for k in keys if k not in done]
    st.set_plan_status(done, "done")
    if not collect:
        st.db.executemany("UPDATE items SET current_album_id=? WHERE item_id=?", [(target_key, k) for k in done])
    if failed:
        st.set_plan_status(failed, "unknown", error="not found in target after move")
        st.db.commit()
        reconcile_keys(st, client, failed)
    st.event("batch_collected" if collect else "batch_moved", album_id=target_key, commit=False, batch_id=batch_id,
             moved=len(done),
             failed=len(failed), count_before=before, count_after=after)
    st.db.commit()
    return done, failed


def cmd_apply(st, cfg, client, args):
    stuck = [r[0] for r in st.q("SELECT item_id FROM plan WHERE status IN ('in_progress','unknown')")]
    if stuck and args.yes:
        log.info("reconciling %d in-progress/unknown items from a previous run", len(stuck))
        reconcile_keys(st, client, stuck)

    targets = [dict(r) for r in st.q(
        "SELECT t.name, t.kind, t.album_id, COUNT(p.item_id) pending, "
        "SUM(p.action='collect') collect FROM targets t "
        "JOIN plan p ON p.target_name=t.name AND p.status='pending' GROUP BY t.name")]
    if args.target:
        targets = [t for t in targets if any(fnmatch.fnmatch(t["name"], pat) for pat in args.target)]
    if args.kind:
        targets = [t for t in targets if t["kind"] in args.kind]
    targets.sort(key=lambda t: (KIND_ORDER.get(t["kind"], 9), t["name"]))
    limit = args.limit
    if not args.yes:
        for t in targets:
            verb = "collect" if t["collect"] == t["pending"] else "move" if not t["collect"] else "move/collect"
            print(f"  would {verb} {t['pending']:5} -> {t['name']} ({t['kind']})")
        total = sum(t["pending"] for t in targets)
        print(f"  {total if limit is None else min(total, limit)} items across {len(targets)} albums"
              + (f" (limit {limit})" if limit is not None else ""))
        if stuck:
            print(f"  plus {len(stuck)} items from an earlier run to check against the server first")
        run.dry_run_footer()
        return None

    run.install_sigint()
    progress = run.Progress(min(sum(t["pending"] for t in targets), limit or 10**12),
                            "collecting" if all(t["collect"] == t["pending"] for t in targets) else "moving")
    folder_uri = client.ensure_folder_path(client.root_folder(), cfg["target_folder"])
    totals = {"moved": 0, "failed": 0, "albums": 0}
    consecutive_failures = 0
    for t in targets:
        if run.Stop.requested or (limit is not None and totals["moved"] + totals["failed"] >= limit):
            break
        target_key = ensure_target(st, cfg, client, folder_uri, t["name"])
        st.db.commit()
        server_count = client.album_item_count(target_key)
        rows = [dict(r) for r in st.q(
            "SELECT p.item_id, p.action, i.serial, i.current_album_id FROM plan p JOIN items i USING(item_id) "
            "WHERE p.target_name=? AND p.status='pending' ORDER BY p.action, i.current_album_id, p.item_id",
            t["name"])]
        if limit is not None:
            rows = rows[:limit - totals["moved"] - totals["failed"]]
        if server_count + len(rows) > cfg["album_hard_cap"]:
            log.error("%r would exceed hard cap (%d on server + %d); skipping", t["name"], server_count, len(rows))
            st.event("cap_exceeded", level="error", album_id=target_key, name=t["name"],
                     server_count=server_count, pending=len(rows))
            continue
        totals["albums"] += 1
        log.info("-> %s: %s %d (server has %d)", t["name"], "collecting" if t["collect"] == len(rows) else "moving",
                 len(rows), server_count)
        bs = args.batch_size or cfg["move_batch_size"]
        # batch within a single source album and action (move | collect)
        i = 0
        while i < len(rows) and not run.Stop.requested:
            src, action = rows[i]["current_album_id"], rows[i]["action"]
            batch = [rows[i]]
            i += 1
            while i < len(rows) and len(batch) < bs and (rows[i]["current_album_id"], rows[i]["action"]) == (src, action):
                batch.append(rows[i])
                i += 1
            done, failed = move_batch(st, client, target_key, batch, uuid.uuid4().hex[:12], collect=action == "collect")
            totals["moved"] += len(done)
            totals["failed"] += len(failed)
            progress.update(len(done) + len(failed), failed=len(failed))
            consecutive_failures = consecutive_failures + 1 if failed and not done else 0
            if consecutive_failures >= cfg["max_consecutive_failures"]:
                log.error("too many consecutive failed batches; stopping")
                run.Stop.requested = True
        count = client.album_item_count(target_key)
        st.db.execute("UPDATE targets SET server_count=?, checked_at=? WHERE name=?", (count, now(), t["name"]))
        st.db.commit()
        log.info("   %s now has %s items (moved so far %d, failed %d)", t["name"], count, totals["moved"],
                 totals["failed"])
    progress.close()
    return totals


# ---------------------------------------------------------------- reconcile

def cmd_reconcile(st, cfg, client, args):
    statuses = ("in_progress", "unknown") + (("failed",) if args.include_failed else ())
    keys = [r[0] for r in st.q(f"SELECT item_id FROM plan WHERE status IN ({','.join('?' * len(statuses))})",
                               *statuses)]
    log.info("reconciling %d items", len(keys))
    return reconcile_keys(st, client, keys)


# ------------------------------------------------------------------- verify

def cmd_verify(st, cfg, client, args):
    """Compare every target album on the server with the plan, and source album counts with the DB."""
    problems = 0
    out = {"targets_ok": 0, "targets_bad": 0, "sources_ok": 0, "sources_bad": 0}
    targets = st.q("SELECT name, album_id FROM targets WHERE album_id IS NOT NULL ORDER BY name")
    for n, t in enumerate(targets, 1):
        log.info("verify %d/%d: %s", n, len(targets), t["name"])
        expected = {r[0] for r in st.q("SELECT item_id FROM plan WHERE target_name=? AND status='done'", t["name"])}
        try:
            server = {it["item_id"] for it in client.list_album_items(t["album_id"], ids_only=True)}
        except NotFound:
            if expected:
                raise
            out.setdefault("targets_deleted", 0)
            out["targets_deleted"] += 1  # e.g. a duplicates album removed by delete-duplicates
            continue
        missing, extra = expected - server, server - expected
        ok = not missing and not extra
        out["targets_ok" if ok else "targets_bad"] += 1
        st.db.execute("UPDATE targets SET server_count=?, checked_at=? WHERE name=?", (len(server), now(), t["name"]))
        st.db.commit()  # don't hold the write lock across a long, network-bound verify
        if not ok:
            problems += 1
            log.error("%s: %d expected, %d on server; missing %s; unexpected %s", t["name"], len(expected),
                      len(server), sorted(missing)[:10], sorted(extra)[:10])
            st.event("verify_mismatch", level="error", album_id=t["album_id"], commit=False, name=t["name"],
                     missing=sorted(missing), extra=sorted(extra))
    for s in st.q("SELECT album_id, path FROM source_albums WHERE rows_stored IS NOT NULL"):
        expected = st.one("SELECT COUNT(*) FROM items WHERE current_album_id=?", s["album_id"])
        try:
            actual = client.album_item_count(s["album_id"])
        except NotFound:
            actual = 0  # deleted by delete-empty-sources; fine as long as nothing is expected there
        ok = expected == actual
        out["sources_ok" if ok else "sources_bad"] += 1
        if not ok:
            log.error("source %s: DB expects %s, server has %s", s["path"], expected, actual)
            st.event("verify_source_mismatch", level="error", album_id=s["album_id"], commit=False,
                     expected=expected, actual=actual)
    st.event("verify", commit=False, **out)
    st.db.commit()
    return out


# ---------------------------------------------------------------- deletions

def cmd_delete_duplicates(st, cfg, client, args):
    """Delete the duplicates album(s) after proving, against the server, that each holds exactly the planned
    duplicate copies, that every copy's kept item (plan.keeper_item_id) is in its target album right now, and that
    no copy is also in another album (deleting it would remove it there too).
    Permanent: requires --yes."""
    targets = st.q("SELECT name, album_id FROM targets WHERE kind='duplicates' AND album_id IS NOT NULL")
    out = {"deleted_albums": 0, "deleted_items": 0}
    listed = {}

    def on_server(album_id):
        if album_id not in listed:
            listed[album_id] = {it["item_id"] for it in client.list_album_items(album_id, ids_only=True)}
        return listed[album_id]

    for t in targets:
        rows = [dict(r) for r in st.q("SELECT item_id, keeper_item_id FROM plan WHERE target_name=? "
                                      "AND action='park_duplicate' AND status='done'", t["name"])]
        expected = {r["item_id"] for r in rows}
        server = on_server(t["album_id"])
        if server != expected:
            raise SystemExit(f"{t['name']}: server contents differ from plan "
                             f"({len(server - expected)} unexpected, {len(expected - server)} missing); not deleting")
        unsafe = []
        for r in rows:
            keeper = st.q("SELECT p.status, p.action, t.album_id FROM plan p JOIN targets t ON t.name=p.target_name "
                          "WHERE p.item_id=?", r["keeper_item_id"]) if r["keeper_item_id"] else []
            ok = (keeper and keeper[0]["status"] == "done" and keeper[0]["action"] in ("move", "collect")
                  and keeper[0]["album_id"] and r["keeper_item_id"] in on_server(keeper[0]["album_id"]))
            if not ok:
                unsafe.append(r["item_id"])
        if unsafe:
            raise SystemExit(f"{t['name']}: {len(unsafe)} copies lack a kept copy that is in place on the server; "
                             "not deleting")
        # Deleting an original also deletes every collected copy of it, so a copy that is in another album too
        # would vanish from there.
        elsewhere = [iid for iid in sorted(expected) if set(client.item_album_ids(iid)) - {t["album_id"]}]
        if elsewhere:
            raise SystemExit(f"{t['name']}: {len(elsewhere)} copies are also in another album (deleting them would "
                             f"remove them there too): {', '.join(elsewhere[:5])}; not deleting")
        log.info("%s: %d copies verified (server == plan, every kept copy in place)", t["name"], len(server))
        if not args.yes:
            log.info("dry run: pass --yes to delete %s", t["name"])
            continue
        client.delete_album(t["album_id"])
        st.set_plan_status(sorted(expected), "deleted")
        st.db.execute("UPDATE items SET current_album_id=NULL WHERE item_id IN "
                      f"({','.join('?' * len(expected))})", tuple(expected))
        st.db.execute("UPDATE targets SET server_count=0, checked_at=? WHERE name=?", (now(), t["name"]))
        st.event("album_deleted", album_id=t["album_id"], commit=False, name=t["name"], items=len(expected),
                 reason="byte-identical duplicates")
        st.db.commit()
        log.info("deleted %s (%s, %d items)", t["name"], t["album_id"], len(expected))
        out["deleted_albums"] += 1
        out["deleted_items"] += len(expected)
    return out


def cmd_delete_empty_sources(st, cfg, client, args):
    """Delete source albums that the server reports as empty. Requires --yes."""
    out = {"deleted": [], "kept_nonempty": []}
    for s in st.q("SELECT album_id, path FROM source_albums ORDER BY path"):
        try:
            count = client.album_item_count(s["album_id"])
        except NotFound:
            log.info("%s already gone", s["path"])
            continue
        if count:
            out["kept_nonempty"].append((s["path"], count))
            log.warning("%s still has %s items; keeping", s["path"], count)
            continue
        if not args.yes:
            log.info("dry run: would delete empty %s", s["path"])
            continue
        client.delete_album(s["album_id"])
        st.event("album_deleted", album_id=s["album_id"], path=s["path"], reason="empty source album")
        log.info("deleted empty source album %s", s["path"])
        out["deleted"].append(s["path"])
    return out


def cmd_delete_empty_targets(st, cfg, client, args):
    """Delete albums this tool created (targets.created_at set) that the server reports as empty and that no
    plan/upload still points to. Albums the tool merely found and reused are never deleted. Requires --yes."""
    out = {"deleted": [], "kept": []}
    rows = st.q("SELECT name, album_id FROM targets WHERE album_id IS NOT NULL AND created_at IS NOT NULL ORDER BY name")
    for t in rows:
        if args.name and not any(fnmatch.fnmatch(t["name"], pat) for pat in args.name):
            continue
        planned = st.one("SELECT COUNT(*) FROM plan WHERE target_name=? AND status IN ('pending','in_progress','done')",
                         t["name"]) + st.one("SELECT COUNT(*) FROM uploads WHERE target_name=? AND status!='skipped'",
                                             t["name"])
        try:
            count = client.album_item_count(t["album_id"])
        except NotFound:
            continue
        if count or planned:
            reason = f"{count} items on the server" if count else f"{planned} items still planned for it"
            out["kept"].append(f"{t['name']}: {reason}")
            continue
        if not args.yes:
            log.info("dry run: would delete empty target album %s", t["name"])
            continue
        client.delete_album(t["album_id"])
        st.db.execute("UPDATE targets SET album_id=NULL, album_ref=NULL, node_ref=NULL, server_count=0, checked_at=? "
                      "WHERE name=?", (now(), t["name"]))
        st.event("album_deleted", album_id=t["album_id"], commit=False, name=t["name"], reason="empty target album")
        st.db.commit()
        log.info("deleted empty target album %s", t["name"])
        out["deleted"].append(t["name"])
    return out


# --------------------------------------------------------------------- undo

def cmd_undo(st, cfg, client, args):
    rows = [dict(r) for r in st.q(
        "SELECT p.item_id, p.action, i.serial, i.src_album_id, t.album_id AS target_key FROM plan p "
        "JOIN items i USING(item_id) JOIN targets t ON t.name=p.target_name "
        "WHERE p.target_name=? AND p.status='done' ORDER BY i.src_album_id", args.target)]
    collected = [r for r in rows if r["action"] == "collect"]
    rows = [r for r in rows if r["action"] != "collect"]
    if not args.yes:
        sources = {r["src_album_id"] for r in rows}
        print(f"  would move {len(rows)} items from {args.target!r} back to {len(sources)} source albums")
        if collected:
            print(f"  would remove {len(collected)} collected copies from {args.target!r} (their originals stay)")
        run.dry_run_footer()
        return None
    run.install_sigint()
    removed = undo_collected(st, client, collected, args.target)
    log.info("moving %d items from %r back to their source albums", len(rows), args.target)
    moved = 0
    for src in sorted({r["src_album_id"] for r in rows}):
        if run.Stop.requested:
            break
        group = [r for r in rows if r["src_album_id"] == src]
        for i in range(0, len(group), cfg["move_batch_size"]):
            batch = group[i:i + cfg["move_batch_size"]]
            client.move_items(src, [client.item_ref(r["target_key"], r["item_id"], r["serial"]) for r in batch])
            keys = [r["item_id"] for r in batch]
            st.set_plan_status(keys, "pending")
            st.db.executemany("UPDATE items SET current_album_id=? WHERE item_id=?", [(src, k) for k in keys])
            st.event("undo_batch", album_id=src, commit=False, target=args.target, size=len(batch))
            st.db.commit()
            moved += len(batch)
    return {"moved_back": moved, "collected_copies_removed": removed}


def undo_collected(st, client, rows, target):
    """Remove collected copies from the target album. Only ever the copy in the target: the original (in the
    source) is never touched, since removing an original deletes every collected copy too."""
    removed = 0
    for r in rows:
        if run.Stop.requested:
            break
        if r["target_key"] == r["src_album_id"]:
            continue   # never the original
        try:
            client.remove_item(client.item_ref(r["target_key"], r["item_id"], r["serial"]))
        except NotFound:
            pass   # already gone from the target
        st.set_plan_status([r["item_id"]], "pending")
        st.event("undo_collect", item_id=r["item_id"], album_id=r["target_key"], commit=False, target=target)
        st.db.commit()
        removed += 1
    return removed
