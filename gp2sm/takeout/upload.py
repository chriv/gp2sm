"""Stage, upload, verify and remove Takeout files recorded in the `uploads` table.

Rows come from `gp2sm takeout plan`:

  stage    stream the archives: convert to JPEG where planned (EXIF kept; a missing capture date filled
           from the Takeout metadata, losslessly) and write other files as they are into the stage folder
  upload   create albums, set filename sort, upload staged files; records the item id per file and
           reconciles ambiguous outcomes against the album before any retry
  verify   compare each touched album on the server with the recorded uploads
  remove   delete specific uploads made by this tool (by id; dry run unless --yes)
"""

import concurrent.futures
import datetime
import fnmatch
import hashlib
import logging
import os
from zoneinfo import ZoneInfo

import piexif

from gp2sm.cli import run
from gp2sm.importer.dedupe import bounded_map
from gp2sm.importer.plan import landed_name
from gp2sm.media.convert import to_jpeg
from gp2sm.organize.engine import ensure_target
from gp2sm.smugmug.client import NotFound, SmugMugError
from gp2sm.state import now
from gp2sm.takeout.archive import read_members

log = logging.getLogger("gp2sm.takeout.upload")



# ------------------------------------------------------------------ pure parts

def exif_datetime_fields(taken_ts, tz_name):
    """Unix seconds -> ('YYYY:MM:DD HH:MM:SS' local, '+HH:MM' offset) for EXIF."""
    dt = datetime.datetime.fromtimestamp(taken_ts, ZoneInfo(tz_name))
    off = dt.utcoffset()
    sign = "-" if off < datetime.timedelta(0) else "+"
    minutes = abs(int(off.total_seconds())) // 60
    return dt.strftime("%Y:%m:%d %H:%M:%S"), f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def fill_exif_date(jpg_path, taken_ts, tz_name):
    """If the JPEG has no DateTimeOriginal, write it (and digitized/offset) losslessly. Returns a note."""
    exif = piexif.load(jpg_path)
    if exif["Exif"].get(piexif.ExifIFD.DateTimeOriginal):
        return "camera date kept"
    if not taken_ts:
        return "no date available"
    when, offset = exif_datetime_fields(taken_ts, tz_name)
    exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = when.encode()
    exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = when.encode()
    exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset.encode()
    exif["0th"][piexif.ImageIFD.DateTime] = when.encode()
    exif.pop("thumbnail", None)
    exif["1st"] = {}
    piexif.insert(piexif.dump(exif), jpg_path)
    return f"date filled from Takeout: {when} {offset}"


# ----------------------------------------------------------------------- stage

def select(st, statuses, args):
    rows = [dict(r) for r in st.q(
        f"SELECT * FROM uploads WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY target_name, upload_name",
        *statuses)]
    if args.target:
        rows = [r for r in rows if any(fnmatch.fnmatch(r["target_name"], p) for p in args.target)]
    if args.limit:
        rows = rows[:args.limit]
    return rows


def convert_still(data, src_ext, dest, taken_ts, tz_name):
    to_jpeg(data, src_ext, dest, quality=92)
    return fill_exif_date(dest, taken_ts, tz_name)


def cmd_stage(st, cfg, args):
    rows = select(st, ("planned",), args)
    if not rows:
        return {"staged": 0}
    os.makedirs(args.stage_dir, exist_ok=True)
    by_member = {(r["archive"], r["src_path"]): r for r in rows}
    done = failed = 0

    def work(row, data):
        dest = os.path.join(args.stage_dir, f"{row['upload_id']}_{row['upload_name']}")
        if row["convert"]:
            note = convert_still(data, os.path.splitext(row["src_path"])[1].lower(), dest, row["taken_ts"],
                                 cfg["timezone"])
        else:   # clips were paired with their stills by time in the plan
            with open(dest, "wb") as f:
                f.write(data)
            note = "as is"
        with open(dest, "rb") as f:
            md5 = hashlib.md5(f.read()).hexdigest()
        return dest, os.path.getsize(dest), md5, note

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for archive in sorted({a for a, _ in by_member}):
            remaining = {p for a, p in by_member if a == archive}
            pairs = ((by_member[(archive, path)], (by_member[(archive, path)], data))
                     for path, data in read_members(os.path.join(args.takeout_dir, archive), remaining))
            for n, (row, fut) in enumerate(bounded_map(pool, lambda a: work(*a), pairs, args.workers * 2), 1):
                if n % 100 == 0:
                    st.db.commit()
                try:
                    dest, size, md5, note = fut.result()
                    st.db.execute("UPDATE uploads SET status='staged', staged_path=?, staged_size=?, staged_md5=?, "
                                  "exif_note=?, updated_at=? WHERE upload_id=?",
                                  (dest, size, md5, note, now(), row["upload_id"]))
                    done += 1
                except Exception as e:
                    st.db.execute("UPDATE uploads SET status='failed', last_error=?, updated_at=? WHERE upload_id=?",
                                  (f"stage: {e!r}"[:500], now(), row["upload_id"]))
                    failed += 1
            st.db.commit()
            log.info("staged from %s: %d ok, %d failed so far", archive, done, failed)
    st.event("stage", staged=done, failed=failed)
    return {"staged": done, "failed": failed}


# ---------------------------------------------------------------------- upload

def find_in_album(client, album_id, filename, md5=None):
    """Return (item_id, item_ref) of an item named `filename` (and, if given, with this md5) in the album."""
    for it in client.list_album_items(album_id):
        if (it["name"] or "").lower() == filename.lower() and (md5 is None or it["md5"] == md5):
            return it["item_id"], it["item_ref"]
    return None


def reconcile_uploads(st, client, rows):
    """For uploads with an unknown outcome, look in the target album before anything is retried."""
    fixed = {"done": 0, "staged": 0}
    for r in rows:
        album_id = st.one("SELECT album_id FROM targets WHERE name=?", r["target_name"])
        caps = client.capabilities
        name = landed_name(r["upload_name"], caps)   # e.g. a kept HEIC lands as .JPG
        ext = os.path.splitext(r["upload_name"])[1].lower()
        md5 = r["staged_md5"] if r["role"] != "clip" and ext in caps.stores_original_bytes else None
        hit = find_in_album(client, album_id, name, md5) if album_id else None
        if hit:
            st.db.execute("UPDATE uploads SET status='done', item_id=?, item_ref=?, last_error=NULL, "
                          "updated_at=? WHERE upload_id=?", (hit[0], hit[1], now(), r["upload_id"]))
            fixed["done"] += 1
        else:
            st.db.execute("UPDATE uploads SET status='staged', updated_at=? WHERE upload_id=?", (now(), r["upload_id"]))
            fixed["staged"] += 1
        st.event("upload_reconciled", commit=False, upload_id=r["upload_id"], found=bool(hit))
    st.db.commit()
    return fixed


def cmd_upload(st, cfg, client, args):
    stuck = [dict(r) for r in st.q("SELECT * FROM uploads WHERE status IN ('uploading','unknown')")]
    if not args.yes:
        rows = select(st, ("staged",), args)
        by_album = {}
        for r in rows:
            by_album[r["target_name"]] = by_album.get(r["target_name"], 0) + 1
        for name, n in sorted(by_album.items()):
            print(f"  would upload {n:5} -> {name}")
        print(f"  {len(rows)} files into {len(by_album)} albums")
        if stuck:
            print(f"  plus {len(stuck)} uploads from an earlier run to check against the server first")
        run.dry_run_footer()
        return None
    if stuck:
        log.info("reconciling %d uploads with unknown outcome", len(stuck))
        reconcile_uploads(st, client, stuck)
    rows = select(st, ("staged",), args)
    if not rows:
        return {"uploaded": 0}
    run.install_sigint()

    folder_uri = client.ensure_folder_path(client.root_folder(), cfg["target_folder"])
    albums = {}
    for name in sorted({r["target_name"] for r in rows}):
        key = ensure_target(st, cfg, client, folder_uri, name)
        st.db.commit()
        albums[name] = key
    log.info("uploading %d files into %d albums", len(rows), len(albums))

    def one(r):
        return client.upload_file(albums[r["target_name"]], r["staged_path"], r["upload_name"], r["content_type"])

    totals = {"uploaded": 0, "failed": 0, "unknown": 0}
    progress = run.Progress(len(rows), "uploading")
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        pending = iter(rows)
        inflight = {}

        def submit_next():
            r = next(pending, None)
            if r is None or run.Stop.requested:
                return False
            st.db.execute("UPDATE uploads SET status='uploading', attempts=attempts+1, updated_at=? WHERE upload_id=?",
                          (now(), r["upload_id"]))
            st.db.commit()
            inflight[pool.submit(one, r)] = r
            return True

        for _ in range(args.workers * 2):
            if not submit_next():
                break
        while inflight:
            fut = next(concurrent.futures.as_completed(inflight))
            r = inflight.pop(fut)
            try:
                item = fut.result()
                st.db.execute("UPDATE uploads SET status='done', item_id=?, item_ref=?, last_error=NULL, "
                              "updated_at=? WHERE upload_id=?", (item["item_id"], item["item_ref"], now(), r["upload_id"]))
                totals["uploaded"] += 1
                progress.update()
            except SmugMugError as e:
                status = "unknown" if e.ambiguous else "failed"
                st.db.execute("UPDATE uploads SET status=?, last_error=?, updated_at=? WHERE upload_id=?",
                              (status, str(e)[:500], now(), r["upload_id"]))
                totals[status] += 1
                progress.update(**{status: 1})
                st.event("upload_error", level="warning", commit=False, upload_id=r["upload_id"], error=str(e))
            st.db.commit()
            submit_next()
    progress.close()
    unknown = [dict(r) for r in st.q("SELECT * FROM uploads WHERE status='unknown'")]
    if unknown:
        totals["reconciled"] = reconcile_uploads(st, client, unknown)
    # Filename sort keeps each Live Photo's JPEG and clip side by side. Applied after uploading so it can
    # never block uploads; it's an album setting, so re-running is harmless.
    for name, key in albums.items():
        try:
            client.set_sort_by_filename(key)
        except SmugMugError as e:
            log.warning("could not set filename sort on %s: %s", name, e)
            st.event("album_sort_failed", level="warning", album_id=key, name=name, error=str(e))
    st.event("upload", **totals)
    return totals


# ---------------------------------------------------------------------- remove

def cmd_remove(st, cfg, client, args):
    """Delete specific uploads this tool made (by upload id) from SmugMug. Dry run unless --yes."""
    ids = [int(x) for x in args.ids]
    rows = [dict(r) for r in st.q(f"SELECT * FROM uploads WHERE upload_id IN ({','.join('?' * len(ids))})", *ids)]
    out = {"removed": 0, "skipped": 0}
    for r in rows:
        if r["status"] != "done" or not r["item_ref"]:
            log.warning("upload %s (%s) is %s; not ours to remove", r["upload_id"], r["upload_name"], r["status"])
            out["skipped"] += 1
            continue
        if not args.yes:
            log.info("dry run: would remove %s from %s", r["upload_name"], r["target_name"])
            continue
        try:
            client.remove_item(r["item_ref"])
        except NotFound:
            log.info("%s already gone", r["upload_name"])
        new_status = "staged"   # uploaded again only by a later `gp2sm takeout upload --yes`
        st.db.execute("UPDATE uploads SET status=?, item_id=NULL, item_ref=NULL, last_error=?, updated_at=? "
                      "WHERE upload_id=?", (new_status, f"removed: {args.reason}", now(), r["upload_id"]))
        st.event("upload_removed", commit=False, upload_id=r["upload_id"], name=r["upload_name"],
                 target=r["target_name"], reason=args.reason)
        st.db.commit()
        out["removed"] += 1
    return out


# ---------------------------------------------------------------------- verify

def cmd_verify(st, cfg, client, args):
    """Confirm uploads on the server. Incremental: only uploads without verified_at (unless --all).

    Lists each affected album once (item ids only), with a few albums in parallel.
    """
    where = "status='done'" + ("" if args.all else " AND verified_at IS NULL")
    rows = st.q(f"SELECT upload_id, target_name, item_id FROM uploads WHERE {where}")
    by_album = {}
    for r in rows:
        if not args.target or any(fnmatch.fnmatch(r["target_name"], p) for p in args.target):
            by_album.setdefault(r["target_name"], []).append((r["upload_id"], r["item_id"]))
    log.info("verifying %d uploads in %d albums", sum(len(v) for v in by_album.values()), len(by_album))

    album_ids = {name: st.one("SELECT album_id FROM targets WHERE name=?", name) for name in by_album}

    def listing(name):  # runs in a worker thread: no database access here
        return name, {it["item_id"] for it in client.list_album_items(album_ids[name], ids_only=True)}

    out = {"albums_ok": 0, "albums_bad": 0, "uploads_verified": 0, "uploads_missing": 0}
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futures = [pool.submit(listing, name) for name in sorted(by_album)]
        for n, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            name, server = fut.result()
            ok = [uid for uid, key in by_album[name] if key in server]
            missing = [key for uid, key in by_album[name] if key not in server]
            st.db.executemany("UPDATE uploads SET verified_at=? WHERE upload_id=?", [(now(), uid) for uid in ok])
            out["uploads_verified"] += len(ok)
            out["uploads_missing"] += len(missing)
            if missing:
                out["albums_bad"] += 1
                log.error("%s: %d uploaded items missing on server: %s", name, len(missing), sorted(missing)[:10])
                st.event("upload_verify_mismatch", level="error", commit=False, name=name, missing=sorted(missing))
            else:
                out["albums_ok"] += 1
            st.db.commit()
            log.info("verify %d/%d: %s (%d ok%s)", n, len(by_album), name, len(ok),
                     f", {len(missing)} MISSING" if missing else "")
    return out


