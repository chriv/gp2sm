"""Consolidate earlier conversion uploads on SmugMug into dated albums.

Pipeline (each step is idempotent and recorded in the state DB):

  inventory      list source albums on SmugMug (with metadata) into `images`
  import-legacy  load Google item lists / MD5s from the legacy transfer DBs
  match          tie each image to a Google item; group exact duplicates; pick keepers
  plan           decide each image's destination album (dated / undated / duplicates)
  report         summarize state
  apply          create albums and move images (batched, verified, resumable)
  reconcile      re-check in-progress/unknown items against the server
  verify         compare every target/source album on the server with the DB
  undo           move a target album's items back to their source albums
  delete-duplicates     delete the duplicates album(s) after server-side checks (--yes)
  delete-empty-sources  delete source albums the server reports as empty (--yes)
  delete-empty-targets  delete albums this tool created that are empty and have nothing planned (--yes)

Usage: python -m gp2sm.consolidate [--config data/consolidate.json] <command> [options]
"""

import argparse
import concurrent.futures
import fnmatch
import json
import logging
import os
import signal
import sqlite3
import sys
import uuid

from gp2sm import planning
from gp2sm.smugmug_client import NotFound, SmugMugClient, SmugMugError
from gp2sm.state import State, now

log = logging.getLogger("gp2sm.consolidate")

DEFAULTS = {
    "smugmug_config": "smugmug_config.json",
    "state_db": "data/consolidation.db",
    "log_file": "data/consolidate.log",
    "legacy_dbs": [],
    "source_album_patterns": [],
    "target_folder": "Consolidated",
    "photo_album_template": "Photos {yyyy}-{mm}",
    "video_album_template": "Videos {yyyy}",
    "undated_photo_album": "Photos Undated",
    "undated_video_album": "Videos Undated",
    "duplicates_album": "Duplicates (review)",
    "timezone": "UTC",
    "album_soft_cap": 4000,
    "album_hard_cap": 5000,
    "move_batch_size": 25,
    "max_consecutive_failures": 5,
}
KIND_ORDER = {"photo": 0, "video": 1, "photo_undated": 2, "video_undated": 3, "duplicates": 4}


def load_config(path):
    cfg = dict(DEFAULTS)
    with open(path) as f:
        cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    return cfg


def setup_logging(log_file, debug=False):
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(fh)
    root.addHandler(ch)
    for noisy in ("urllib3", "requests_oauthlib", "oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- inventory

def cmd_inventory(st, cfg, client, args):
    patterns = [p.lower() for p in cfg["source_album_patterns"]]
    if not patterns:
        raise SystemExit("config: source_album_patterns is empty")
    target_prefix = "/" + cfg["target_folder"].strip("/").lower() + "/"
    albums = [a for a in client.list_albums()
              if any(p in (a["path"] or "").lower() for p in patterns)
              and not (a["path"] or "").lower().startswith(target_prefix)]
    log.info("%d source albums match %s", len(albums), cfg["source_album_patterns"])
    for a in albums:
        st.db.execute("INSERT INTO source_albums(album_key, album_uri, name, url_path, image_count) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(album_key) DO UPDATE SET name=excluded.name, url_path=excluded.url_path, "
                      "image_count=excluded.image_count",
                      (a["album_id"], a["ref"], a["name"], a["path"], a["item_count"]))
    st.db.commit()

    def fetch(album):
        return album, list(client.list_album_items(album["album_id"], with_metadata=True))

    total = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="inv") as pool:
        for album, items in pool.map(fetch, [a for a in albums if a["item_count"]]):
            key = album["album_id"]
            ts = now()
            with st.db:
                for it in items:
                    st.db.execute(
                        "INSERT INTO images(image_key, serial, src_album_key, src_album_image_uri, current_album_key,"
                        " filename, format, is_video, archived_md5, archived_size, width, height, duration_s,"
                        " uploaded, capture_dt_smug, raw_image, raw_metadata, first_seen, last_seen)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(image_key) DO UPDATE SET current_album_key=excluded.current_album_key,"
                        " archived_md5=excluded.archived_md5, raw_image=excluded.raw_image,"
                        " raw_metadata=excluded.raw_metadata, last_seen=excluded.last_seen",
                        (it["item_id"], it["serial"], key, it["item_ref"], key, it["name"], it["format"],
                         int(it["is_video"]), it["md5"], it["size"], it["width"], it["height"], it["duration_s"],
                         it["uploaded"], it["capture_time"], json.dumps(it["raw"]),
                         json.dumps(it["raw_metadata"]) if it["raw_metadata"] else None, ts, ts))
                st.db.execute("UPDATE source_albums SET rows_stored=?, inventoried_at=? WHERE album_key=?",
                              (len(items), ts, key))
                st.event("inventory_album", album_key=key, commit=False, rows=len(items),
                         item_count=album["item_count"], path=album["path"])
            flag = "" if len(items) == album["item_count"] else f" (album reports {album['item_count']})"
            log.info("inventoried %s %s: %d%s", key, album["path"], len(items), flag)
            total += len(items)
    return {"albums": len(albums), "images": total}


# ------------------------------------------------------------ import-legacy

def cmd_import_legacy(st, cfg, client, args):
    stats = {}
    for path in cfg["legacy_dbs"]:
        src = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "media_items" not in tables:
            log.warning("%s has no media_items table; skipped", path)
            continue
        n = n_md5 = 0
        with st.db:
            for r in src.execute("SELECT * FROM media_items"):
                md = json.loads(r["media_metadata_json"] or "{}")
                st.db.execute(
                    "INSERT INTO legacy_items(google_id, filename, mime_type, creation_ts, width, height, product_url,"
                    " legacy_status, source_db) VALUES(?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(google_id) DO UPDATE SET"
                    " creation_ts=COALESCE(legacy_items.creation_ts, excluded.creation_ts),"
                    " width=COALESCE(legacy_items.width, excluded.width),"
                    " height=COALESCE(legacy_items.height, excluded.height)",
                    (r["google_id"], r["filename"], r["mime_type"],
                     r["creation_timestamp"] or md.get("creationTime"),
                     int(md["width"]) if md.get("width") else None, int(md["height"]) if md.get("height") else None,
                     r["product_url"], r["status"], os.path.basename(path)))
                n += 1
                if r["md5_hash"]:
                    st.db.execute("INSERT OR IGNORE INTO legacy_md5 VALUES(?,?,?)",
                                  (r["google_id"], r["md5_hash"], os.path.basename(path)))
                    n_md5 += 1
            st.event("import_legacy", commit=False, path=os.path.basename(path), items=n, md5=n_md5)
        stats[os.path.basename(path)] = {"items": n, "md5": n_md5}
        log.info("imported %s: %d items, %d md5", path, n, n_md5)
    stats["distinct_items"] = st.one("SELECT COUNT(*) FROM legacy_items")
    stats["distinct_md5"] = st.one("SELECT COUNT(DISTINCT md5) FROM legacy_md5")
    return stats


# -------------------------------------------------------------------- match

def load_images(st):
    return [dict(r) for r in st.q("SELECT image_key AS item_id, filename, is_video, archived_md5 AS md5, width, height, "
                                  "uploaded FROM images")]


def cmd_match(st, cfg, client, args):
    index = planning.LegacyIndex([dict(r) for r in st.q("SELECT * FROM legacy_items")],
                                 [dict(r) for r in st.q("SELECT google_id, md5 FROM legacy_md5")])
    images = load_images(st)
    matches = {img["item_id"]: planning.match_image(img, index, cfg["timezone"]) for img in images}
    groups = planning.group_duplicates(images, matches)
    with st.db:
        st.db.execute("DELETE FROM matches")
        st.db.executemany("INSERT INTO matches VALUES(?,?,?,?,?,?,?,?)",
                          [(k, m["google_id"], m["method"], m["confidence"], m["capture_ts_utc"],
                            m["capture_local"], m["candidates"], m["notes"]) for k, m in matches.items()])
        st.db.execute("DELETE FROM dup_groups")
        st.db.executemany("INSERT INTO dup_groups VALUES(?,?,?,?,?)",
                          [(k, g["group_key"], g["group_size"], g["keeper_item_id"], g["is_keeper"])
                           for k, g in groups.items()])
    summary = {r[0]: r[1] for r in st.q("SELECT method, COUNT(*) FROM matches GROUP BY method")}
    summary["keepers"] = st.one("SELECT COUNT(*) FROM dup_groups WHERE is_keeper=1")
    summary["duplicates"] = st.one("SELECT COUNT(*) FROM dup_groups WHERE is_keeper=0")
    st.event("match", **summary)
    return summary


# --------------------------------------------------------------------- plan

def cmd_plan(st, cfg, client, args):
    images = [dict(r) for r in st.q("SELECT image_key AS item_id, filename, is_video, archived_md5 AS md5, uploaded "
                                    "FROM images")]
    matches = {r["image_key"]: dict(r) for r in st.q("SELECT * FROM matches")}
    groups = {r["item_id"]: dict(r) for r in st.q(
        "SELECT image_key AS item_id, group_key, group_size, keeper_image_key AS keeper_item_id, is_keeper FROM dup_groups")}
    if not groups:
        raise SystemExit("run `match` first")
    existing = {r["item_id"]: dict(r) for r in st.q("SELECT image_key AS item_id, status, target_name FROM plan")}
    rows = planning.plan_actions(images, matches, groups, cfg, existing)
    ts = now()
    changed = 0
    with st.db:
        for r in rows:
            prior = existing.get(r["item_id"])
            if prior and prior["status"] in ("done", "in_progress", "unknown"):
                continue  # never re-plan work that has started
            if prior and prior["target_name"] == r["target_name"] and prior["status"] in ("pending", "failed"):
                st.db.execute("UPDATE plan SET action=?, reason=? WHERE image_key=?",
                              (r["action"], r["reason"], r["item_id"]))
                continue
            st.db.execute("INSERT INTO plan(image_key, action, target_name, reason, status, planned_at, updated_at)"
                          " VALUES(?,?,?,?,'pending',?,?) ON CONFLICT(image_key) DO UPDATE SET"
                          " action=excluded.action, target_name=excluded.target_name, reason=excluded.reason,"
                          " status='pending', updated_at=excluded.updated_at",
                          (r["item_id"], r["action"], r["target_name"], r["reason"], ts, ts))
            changed += 1
        kinds = {r["target_name"]: r["kind"] for r in rows}
        for name, planned in st.q("SELECT target_name, COUNT(*) FROM plan GROUP BY target_name"):
            st.db.execute("INSERT INTO targets(name, kind, planned) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET"
                          " planned=excluded.planned, kind=COALESCE(excluded.kind, targets.kind)",
                          (name, kinds.get(name), planned))
        st.db.execute("DELETE FROM targets WHERE album_key IS NULL AND name NOT IN (SELECT target_name FROM plan)")
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
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
        for r in rows:
            print("  " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))

    table("Source albums", "SELECT url_path, image_count, rows_stored FROM source_albums ORDER BY url_path")
    table("Match methods", "SELECT m.method, m.confidence, SUM(i.is_video=0) photos, SUM(i.is_video) videos "
                           "FROM matches m JOIN images i USING(image_key) GROUP BY 1,2 ORDER BY 3 DESC")
    table("Plan status", "SELECT action, status, COUNT(*) n FROM plan GROUP BY 1,2 ORDER BY 1,2")
    if args.targets:
        table("Targets", "SELECT t.name, t.kind, t.album_key, t.planned, "
                         "SUM(p.status='done') done, SUM(p.status='failed') failed, t.server_count "
                         "FROM targets t LEFT JOIN plan p ON p.target_name=t.name GROUP BY t.name ORDER BY t.name")
    else:
        table("Targets by kind", "SELECT kind, COUNT(*) albums, SUM(planned) items, MIN(name) first, MAX(name) last "
                                 "FROM targets GROUP BY kind")
    table("Recent failures", "SELECT image_key, target_name, attempts, substr(last_error,1,90) err FROM plan "
                             "WHERE status='failed' ORDER BY updated_at DESC LIMIT 10")
    return None


# -------------------------------------------------------------------- apply

class Stop:
    requested = False


def _install_sigint():
    def handler(sig, frame):
        if Stop.requested:
            raise KeyboardInterrupt
        Stop.requested = True
        log.warning("stop requested; finishing current batch (Ctrl-C again to abort immediately)")
    signal.signal(signal.SIGINT, handler)


def ensure_target(st, cfg, client, folder_node_uri, name):
    row = st.q("SELECT * FROM targets WHERE name=?", name)[0]
    if row["album_key"]:
        return row["album_key"]
    album_key, album_uri, node_uri, created = client.ensure_album(folder_node_uri, name)
    st.db.execute("UPDATE targets SET album_key=?, album_uri=?, node_uri=?, created_at=? WHERE name=?",
                  (album_key, album_uri, node_uri, now() if created else None, name))
    st.event("album_created" if created else "album_found", album_key=album_key, name=name)
    log.info("%s album %r -> %s", "created" if created else "found existing", name, album_key)
    return album_key


def reconcile_keys(st, client, keys):
    """Ask the server where each image is now and fix plan/images accordingly."""
    fixed = {"done": 0, "pending": 0, "failed": 0}
    for key in keys:
        row = st.q("SELECT p.target_name, t.album_key AS target_key, i.serial, i.src_album_key "
                   "FROM plan p JOIN images i USING(image_key) LEFT JOIN targets t ON t.name=p.target_name "
                   "WHERE p.image_key=?", key)[0]
        try:
            where = client.item_album_ids(key, row["serial"])
        except NotFound:
            where = []
        if row["target_key"] and row["target_key"] in where:
            status, current = "done", row["target_key"]
        elif where:
            status, current = "pending", where[0]
        else:
            status, current = "failed", None
        st.db.execute("UPDATE plan SET status=?, last_error=?, updated_at=? WHERE image_key=?",
                      (status, None if status != "failed" else "image not found in any album", now(), key))
        if current:
            st.db.execute("UPDATE images SET current_album_key=? WHERE image_key=?", (current, key))
        st.event("reconciled", image_key=key, album_key=current, commit=False, status=status, albums=where)
        fixed[status] += 1
    st.db.commit()
    return fixed


def move_batch(st, client, target_key, items, batch_id):
    """Move items (list of plan/image rows) into target. Returns (done_keys, failed_keys)."""
    keys = [r["image_key"] for r in items]
    st.set_plan_status(keys, "in_progress", batch_id=batch_id, bump_attempts=True)
    st.db.commit()
    refs = [client.item_ref(r["current_album_key"], r["image_key"], r["serial"]) for r in items]
    before = client.album_item_count(target_key)
    try:
        client.move_items(target_key, refs)
    except SmugMugError as e:
        if e.http_status == 400 and not e.ambiguous:
            # Batch moves are all-or-nothing: nothing moved. Isolate the bad item(s).
            st.set_plan_status(keys, "pending")
            st.event("batch_rejected", level="warning", album_key=target_key, commit=False,
                     batch_id=batch_id, size=len(items), error=str(e))
            if len(items) == 1:
                # Confirm with the server before calling it failed (it may already be in the target).
                reconcile_keys(st, client, keys)
                if st.one("SELECT status FROM plan WHERE image_key=?", keys[0]) == "done":
                    return keys, []
                st.set_plan_status(keys, "failed", error=str(e)[:500])
                st.db.commit()
                return [], keys
            done, failed = [], []
            for it in items:
                d, f = move_batch(st, client, target_key, [it], f"{batch_id}.{it['image_key']}")
                done += d
                failed += f
            return done, failed
        # Unknown outcome (network/5xx after retries): ask the server.
        st.set_plan_status(keys, "unknown", error=str(e)[:500])
        st.event("batch_unknown", level="error", album_key=target_key, commit=False, batch_id=batch_id, error=str(e))
        st.db.commit()
        reconcile_keys(st, client, keys)
        status = {k: st.one("SELECT status FROM plan WHERE image_key=?", k) for k in keys}
        # 'pending' ones (still in source) are simply retried on a later run
        return [k for k in keys if status[k] == "done"], [k for k in keys if status[k] == "failed"]

    after = client.album_item_count(target_key)
    if after - before == len(items):
        done = keys
    else:
        st.event("count_mismatch", level="warning", album_key=target_key, commit=False,
                 batch_id=batch_id, before=before, after=after, size=len(items))
        done = [r["image_key"] for r in items if client.album_contains(target_key, r["image_key"], r["serial"])]
    failed = [k for k in keys if k not in done]
    st.set_plan_status(done, "done")
    st.db.executemany("UPDATE images SET current_album_key=? WHERE image_key=?", [(target_key, k) for k in done])
    if failed:
        st.set_plan_status(failed, "unknown", error="not found in target after move")
        st.db.commit()
        reconcile_keys(st, client, failed)
    st.event("batch_moved", album_key=target_key, commit=False, batch_id=batch_id, moved=len(done),
             failed=len(failed), count_before=before, count_after=after)
    st.db.commit()
    return done, failed


def cmd_apply(st, cfg, client, args):
    stuck = [r[0] for r in st.q("SELECT image_key FROM plan WHERE status IN ('in_progress','unknown')")]
    if stuck and not args.dry_run:
        log.info("reconciling %d in-progress/unknown items from a previous run", len(stuck))
        reconcile_keys(st, client, stuck)

    targets = [dict(r) for r in st.q(
        "SELECT t.name, t.kind, t.album_key, COUNT(p.image_key) pending FROM targets t "
        "JOIN plan p ON p.target_name=t.name AND p.status='pending' GROUP BY t.name")]
    if args.target:
        targets = [t for t in targets if any(fnmatch.fnmatch(t["name"], pat) for pat in args.target)]
    if args.kind:
        targets = [t for t in targets if t["kind"] in args.kind]
    targets.sort(key=lambda t: (KIND_ORDER.get(t["kind"], 9), t["name"]))
    limit = args.limit
    if args.dry_run:
        for t in targets:
            print(f"  would move {t['pending']:5} -> {t['name']} ({t['kind']})")
        print(f"  {sum(t['pending'] for t in targets)} items across {len(targets)} albums (limit {limit})")
        return None

    _install_sigint()
    folder_uri = client.ensure_folder_path(client.root_folder(), cfg["target_folder"])
    totals = {"moved": 0, "failed": 0, "albums": 0}
    consecutive_failures = 0
    for t in targets:
        if Stop.requested or (limit is not None and totals["moved"] + totals["failed"] >= limit):
            break
        target_key = ensure_target(st, cfg, client, folder_uri, t["name"])
        st.db.commit()
        server_count = client.album_item_count(target_key)
        rows = [dict(r) for r in st.q(
            "SELECT p.image_key, i.serial, i.current_album_key FROM plan p JOIN images i USING(image_key) "
            "WHERE p.target_name=? AND p.status='pending' ORDER BY i.current_album_key, p.image_key", t["name"])]
        if limit is not None:
            rows = rows[:limit - totals["moved"] - totals["failed"]]
        if server_count + len(rows) > cfg["album_hard_cap"]:
            log.error("%r would exceed hard cap (%d on server + %d); skipping", t["name"], server_count, len(rows))
            st.event("cap_exceeded", level="error", album_key=target_key, name=t["name"],
                     server_count=server_count, pending=len(rows))
            continue
        totals["albums"] += 1
        log.info("-> %s: moving %d (server has %d)", t["name"], len(rows), server_count)
        bs = args.batch_size or cfg["move_batch_size"]
        # batch within a single source album
        i = 0
        while i < len(rows) and not Stop.requested:
            src = rows[i]["current_album_key"]
            batch = [rows[i]]
            i += 1
            while i < len(rows) and len(batch) < bs and rows[i]["current_album_key"] == src:
                batch.append(rows[i])
                i += 1
            done, failed = move_batch(st, client, target_key, batch, uuid.uuid4().hex[:12])
            totals["moved"] += len(done)
            totals["failed"] += len(failed)
            consecutive_failures = consecutive_failures + 1 if failed and not done else 0
            if consecutive_failures >= cfg["max_consecutive_failures"]:
                log.error("too many consecutive failed batches; stopping")
                Stop.requested = True
        count = client.album_item_count(target_key)
        st.db.execute("UPDATE targets SET server_count=?, checked_at=? WHERE name=?", (count, now(), t["name"]))
        st.db.commit()
        log.info("   %s now has %s items (moved so far %d, failed %d)", t["name"], count, totals["moved"],
                 totals["failed"])
    return totals


# ---------------------------------------------------------------- reconcile

def cmd_reconcile(st, cfg, client, args):
    statuses = ("in_progress", "unknown") + (("failed",) if args.include_failed else ())
    keys = [r[0] for r in st.q(f"SELECT image_key FROM plan WHERE status IN ({','.join('?' * len(statuses))})",
                               *statuses)]
    log.info("reconciling %d items", len(keys))
    return reconcile_keys(st, client, keys)


# ------------------------------------------------------------------- verify

def cmd_verify(st, cfg, client, args):
    """Compare every target album on the server with the plan, and source album counts with the DB."""
    problems = 0
    out = {"targets_ok": 0, "targets_bad": 0, "sources_ok": 0, "sources_bad": 0}
    targets = st.q("SELECT name, album_key FROM targets WHERE album_key IS NOT NULL ORDER BY name")
    for n, t in enumerate(targets, 1):
        log.info("verify %d/%d: %s", n, len(targets), t["name"])
        expected = {r[0] for r in st.q("SELECT image_key FROM plan WHERE target_name=? AND status='done'", t["name"])}
        try:
            server = {it["item_id"] for it in client.list_album_items(t["album_key"], ids_only=True)}
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
            st.event("verify_mismatch", level="error", album_key=t["album_key"], commit=False, name=t["name"],
                     missing=sorted(missing), extra=sorted(extra))
    for s in st.q("SELECT album_key, url_path FROM source_albums WHERE rows_stored IS NOT NULL"):
        expected = st.one("SELECT COUNT(*) FROM images WHERE current_album_key=?", s["album_key"])
        try:
            actual = client.album_item_count(s["album_key"])
        except NotFound:
            actual = 0  # deleted by delete-empty-sources; fine as long as nothing is expected there
        ok = expected == actual
        out["sources_ok" if ok else "sources_bad"] += 1
        if not ok:
            log.error("source %s: DB expects %s, server has %s", s["url_path"], expected, actual)
            st.event("verify_source_mismatch", level="error", album_key=s["album_key"], commit=False,
                     expected=expected, actual=actual)
    st.event("verify", commit=False, **out)
    st.db.commit()
    return out


# ---------------------------------------------------------------- deletions

def cmd_delete_duplicates(st, cfg, client, args):
    """Delete the duplicates review album(s) after proving, against the server, that each holds
    exactly the planned duplicate copies and that every copy's keeper is safely in a non-duplicate
    target album. Permanent: requires --yes."""
    targets = st.q("SELECT name, album_key FROM targets WHERE kind='duplicates' AND album_key IS NOT NULL")
    target_keys = {r[0] for r in st.q("SELECT album_key FROM targets WHERE kind!='duplicates' AND album_key IS NOT NULL")}
    out = {"deleted_albums": 0, "deleted_items": 0}
    for t in targets:
        expected = {r[0] for r in st.q("SELECT image_key FROM plan WHERE target_name=? AND action='park_duplicate' "
                                       "AND status='done'", t["name"])}
        server = {it["item_id"] for it in client.list_album_items(t["album_key"], ids_only=True)}
        if server != expected:
            raise SystemExit(f"{t['name']}: server contents differ from plan "
                             f"({len(server - expected)} unexpected, {len(expected - server)} missing); not deleting")
        bad_keepers = st.q(
            "SELECT d.image_key FROM dup_groups d JOIN plan kp ON kp.image_key=d.keeper_image_key "
            "JOIN images ki ON ki.image_key=d.keeper_image_key "
            f"WHERE d.image_key IN ({','.join('?' * len(expected))}) "
            "AND (kp.status!='done' OR kp.action!='move')", *expected) if expected else []
        keeper_albums = {r[0] for r in st.q(
            "SELECT DISTINCT ki.current_album_key FROM dup_groups d JOIN images ki ON ki.image_key=d.keeper_image_key "
            f"WHERE d.image_key IN ({','.join('?' * len(expected))})", *expected)} if expected else set()
        if bad_keepers or not keeper_albums <= target_keys:
            raise SystemExit(f"{t['name']}: {len(bad_keepers)} copies lack a safely placed keeper; not deleting")
        log.info("%s: %d copies verified (server == plan, all keepers placed)", t["name"], len(server))
        if not args.yes:
            log.info("dry run: pass --yes to delete %s", t["name"])
            continue
        client.delete_album(t["album_key"])
        st.set_plan_status(sorted(expected), "deleted")
        st.db.execute("UPDATE images SET current_album_key=NULL WHERE image_key IN "
                      f"({','.join('?' * len(expected))})", tuple(expected))
        st.db.execute("UPDATE targets SET server_count=0, checked_at=? WHERE name=?", (now(), t["name"]))
        st.event("album_deleted", album_key=t["album_key"], commit=False, name=t["name"], items=len(expected),
                 reason="byte-identical duplicates")
        st.db.commit()
        log.info("deleted %s (%s, %d items)", t["name"], t["album_key"], len(expected))
        out["deleted_albums"] += 1
        out["deleted_items"] += len(expected)
    return out


def cmd_delete_empty_sources(st, cfg, client, args):
    """Delete source albums that the server reports as empty. Requires --yes."""
    out = {"deleted": [], "kept_nonempty": []}
    for s in st.q("SELECT album_key, url_path FROM source_albums ORDER BY url_path"):
        try:
            count = client.album_item_count(s["album_key"])
        except NotFound:
            log.info("%s already gone", s["url_path"])
            continue
        if count:
            out["kept_nonempty"].append((s["url_path"], count))
            log.warning("%s still has %s items; keeping", s["url_path"], count)
            continue
        if not args.yes:
            log.info("dry run: would delete empty %s", s["url_path"])
            continue
        client.delete_album(s["album_key"])
        st.event("album_deleted", album_key=s["album_key"], url_path=s["url_path"], reason="empty source album")
        log.info("deleted empty source album %s", s["url_path"])
        out["deleted"].append(s["url_path"])
    return out


def cmd_delete_empty_targets(st, cfg, client, args):
    """Delete albums this tool created (targets.created_at set) that the server reports as empty and that no
    plan/upload still points to. Albums the tool merely found and reused are never deleted. Requires --yes."""
    out = {"deleted": [], "kept": []}
    rows = st.q("SELECT name, album_key FROM targets WHERE album_key IS NOT NULL AND created_at IS NOT NULL ORDER BY name")
    for t in rows:
        if args.name and not any(fnmatch.fnmatch(t["name"], pat) for pat in args.name):
            continue
        planned = st.one("SELECT COUNT(*) FROM plan WHERE target_name=? AND status IN ('pending','in_progress','done')",
                         t["name"]) + st.one("SELECT COUNT(*) FROM uploads WHERE target_name=? AND status!='skipped'",
                                             t["name"])
        try:
            count = client.album_item_count(t["album_key"])
        except NotFound:
            continue
        if count or planned:
            if args.name:
                out["kept"].append((t["name"], count, planned))
            continue
        if not args.yes:
            log.info("dry run: would delete empty target album %s", t["name"])
            continue
        client.delete_album(t["album_key"])
        st.db.execute("UPDATE targets SET album_key=NULL, album_uri=NULL, node_uri=NULL, server_count=0, checked_at=? "
                      "WHERE name=?", (now(), t["name"]))
        st.event("album_deleted", album_key=t["album_key"], commit=False, name=t["name"], reason="empty target album")
        st.db.commit()
        log.info("deleted empty target album %s", t["name"])
        out["deleted"].append(t["name"])
    return out


# --------------------------------------------------------------------- undo

def cmd_undo(st, cfg, client, args):
    rows = [dict(r) for r in st.q(
        "SELECT p.image_key, i.serial, i.src_album_key, t.album_key AS target_key FROM plan p "
        "JOIN images i USING(image_key) JOIN targets t ON t.name=p.target_name "
        "WHERE p.target_name=? AND p.status='done' ORDER BY i.src_album_key", args.target)]
    log.info("moving %d items from %r back to their source albums", len(rows), args.target)
    moved = 0
    for src in sorted({r["src_album_key"] for r in rows}):
        group = [r for r in rows if r["src_album_key"] == src]
        for i in range(0, len(group), cfg["move_batch_size"]):
            batch = group[i:i + cfg["move_batch_size"]]
            client.move_items(src, [client.item_ref(r["target_key"], r["image_key"], r["serial"]) for r in batch])
            keys = [r["image_key"] for r in batch]
            st.set_plan_status(keys, "pending")
            st.db.executemany("UPDATE images SET current_album_key=? WHERE image_key=?", [(src, k) for k in keys])
            st.event("undo_batch", album_key=src, commit=False, target=args.target, size=len(batch))
            st.db.commit()
            moved += len(batch)
    return {"moved_back": moved}


# --------------------------------------------------------------------- main

COMMANDS = {
    "inventory": cmd_inventory, "import-legacy": cmd_import_legacy, "match": cmd_match, "plan": cmd_plan,
    "report": cmd_report, "apply": cmd_apply, "reconcile": cmd_reconcile, "verify": cmd_verify, "undo": cmd_undo,
    "delete-duplicates": cmd_delete_duplicates, "delete-empty-sources": cmd_delete_empty_sources,
    "delete-empty-targets": cmd_delete_empty_targets,
}
NEEDS_CLIENT = {"inventory", "apply", "reconcile", "verify", "undo", "delete-duplicates", "delete-empty-sources",
                "delete-empty-targets"}


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm.consolidate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="data/consolidate.json")
    p.add_argument("--debug", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("inventory")
    s.add_argument("--workers", type=int, default=4)
    sub.add_parser("import-legacy")
    sub.add_parser("match")
    sub.add_parser("plan")
    s = sub.add_parser("report")
    s.add_argument("--targets", action="store_true", help="list every target album")
    s = sub.add_parser("apply")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--limit", type=int, help="max items to move this run")
    s.add_argument("--target", action="append", help="only these target album names (glob ok; repeatable)")
    s.add_argument("--kind", action="append", choices=list(KIND_ORDER), help="only these target kinds")
    s.add_argument("--batch-size", type=int)
    s = sub.add_parser("reconcile")
    s.add_argument("--include-failed", action="store_true")
    sub.add_parser("verify")
    for name in ("delete-duplicates", "delete-empty-sources", "delete-empty-targets"):
        s = sub.add_parser(name)
        s.add_argument("--yes", action="store_true", help="actually delete (permanent); default is a dry run")
        if name == "delete-empty-targets":
            s.add_argument("--name", action="append", help="only target albums matching this glob (repeatable)")
    s = sub.add_parser("undo")
    s.add_argument("target", help="exact target album name to move back out")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg["log_file"], args.debug)
    st = State(cfg["state_db"])
    client = SmugMugClient.from_config_file(cfg["smugmug_config"]) if args.command in NEEDS_CLIENT else None
    st.start_run(args.command, vars(args))
    try:
        summary = COMMANDS[args.command](st, cfg, client, args)
    except BaseException as e:
        st.db.rollback()
        st.event("run_failed", level="error", error=repr(e))
        st.finish_run("failed", {"error": repr(e)})
        raise
    status = "stopped" if Stop.requested else "ok"
    st.finish_run(status, summary)
    if summary is not None:
        print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
