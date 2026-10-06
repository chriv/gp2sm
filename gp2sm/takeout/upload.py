"""Upload Live Photos (JPEG still + motion clip, side by side) and remaining HEIC stills from Google Takeout.

Goal per Live Photo: one JPEG and one MP4 clip with the same base name in the same album, which is
sorted by filename so each pair sits together. A still is uploaded only when no SmugMug copy is known
(linked via the legacy bridge or an applied content match); otherwise only its clip is added next to
the existing JPEG.

  plan     decide what to upload, target albums and exact filenames (idempotent)
  stage    stream the archives: HEIC -> JPEG (sips, EXIF kept; missing capture date filled from the
           Takeout sidecar, losslessly) and write clips as-is into data/stage/
  upload   create albums, set filename sort, upload staged files; records the item id per file and
           reconciles ambiguous outcomes against the album before any retry
  verify   compare each touched album on the server with the recorded uploads
  remove   delete specific uploads made by this tool (by id; dry run unless --yes)
  report   summarize

Usage: gp2sm takeout-upload [--config data/consolidate.json] <command> [--target GLOB] [--limit N]
"""

import argparse
import concurrent.futures
import datetime
import fnmatch
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import tarfile
from zoneinfo import ZoneInfo

import piexif

from gp2sm.media.convert import to_jpeg
from gp2sm.organize import planning
from gp2sm.organize.consolidate import ensure_target, load_config, setup_logging
from gp2sm.smugmug.client import NotFound, SmugMugClient, SmugMugError
from gp2sm.state import State, now

log = logging.getLogger("gp2sm.takeout.upload")

HEIC = (".heic", ".heif")
ON_SMUGMUG = ("on_smugmug", "on_smugmug_content_match", "on_smugmug_by_hash", "on_smugmug_inferred_same_name_group")
CONTENT_TYPES = {".JPG": "image/jpeg", ".MP4": "video/mp4"}


# ------------------------------------------------------------------ pure parts

def stem_of(filename):
    return os.path.splitext(filename or "")[0]


def unique_name(name, taken):
    """Return name, or name with ' (k)' before the extension if `taken` (a set of lowercase names) has it."""
    if name.lower() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    k = 1
    while f"{stem} ({k}){ext}".lower() in taken:
        k += 1
    return f"{stem} ({k}){ext}"


def exif_datetime_fields(taken_ts, tz_name):
    """Unix seconds -> ('YYYY:MM:DD HH:MM:SS' local, '+HH:MM' offset) for EXIF."""
    dt = datetime.datetime.fromtimestamp(taken_ts, ZoneInfo(tz_name))
    off = dt.utcoffset()
    sign = "-" if off < datetime.timedelta(0) else "+"
    minutes = abs(int(off.total_seconds())) // 60
    return dt.strftime("%Y:%m:%d %H:%M:%S"), f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


QT_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01
MAX_CLIP_SKEW = 120  # a Live Photo clip starts within seconds of its still


APPLE_DATE = re.compile(rb"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)([+-]\d{4}|Z)")


def clip_creation_ts(data):
    """Capture time of a clip: Apple's com.apple.quicktime.creationdate if present (the true capture time),
    else the mvhd creation time (which can be a later re-encode time)."""
    if b"com.apple.quicktime.creationdate" in data:
        m = APPLE_DATE.search(data)
        if m:
            text = m.group(0).decode()
            text = text.replace("Z", "+00:00") if text.endswith("Z") else text[:-2] + ":" + text[-2:]
            return int(datetime.datetime.fromisoformat(text).timestamp())
    return mp4_creation_ts(data)


def mp4_creation_ts(data):
    """Unix creation time from the first 'mvhd' atom of an MP4/MOV (None if absent or zero)."""
    i = data.find(b"mvhd")
    if i < 0 or i + 16 > len(data):
        return None
    version = data[i + 4]
    raw = int.from_bytes(data[i + 8:i + 16], "big") if version == 1 else int.from_bytes(data[i + 8:i + 12], "big")
    return raw - QT_EPOCH_OFFSET if raw else None


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
    idx = sqlite3.connect(f"file:{os.path.abspath(args.index)}?mode=ro", uri=True)
    taken = dict(idx.execute("SELECT item_id, taken_ts FROM items").fetchall())
    os.makedirs(args.stage_dir, exist_ok=True)
    by_member = {(r["archive"], r["src_path"]): r for r in rows}
    done = failed = 0

    def work(row, data):
        dest = os.path.join(args.stage_dir, f"{row['upload_id']}_{row['upload_name']}")
        if row["role"] == "still":
            note = convert_still(data, os.path.splitext(row["src_path"])[1].lower(), dest,
                                 taken.get(row["item_id"]), cfg["timezone"])
        else:
            clip_ts = clip_creation_ts(data)
            still_ts = taken.get(row.get("pair_item_id") or row["item_id"])  # re-paired clips use their new still
            paired = row["target_name"] != cfg["undated_video_album"]  # unsorted clips aren't next to a still
            if paired and clip_ts and still_ts and abs(clip_ts - still_ts) > MAX_CLIP_SKEW:
                raise PairingError(f"clip created {clip_ts - still_ts:+d}s from its still; likely not its pair")
            with open(dest, "wb") as f:
                f.write(data)
            note = f"clip time {clip_ts - still_ts:+d}s from still" if clip_ts and still_ts else "clip time unknown"
        with open(dest, "rb") as f:
            md5 = hashlib.md5(f.read()).hexdigest()
        return dest, os.path.getsize(dest), md5, note

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for archive in sorted({a for a, _ in by_member}):
            futures = {}
            remaining = {p for a, p in by_member if a == archive}
            with tarfile.open(os.path.join(args.takeout_dir, archive), "r|*") as tar:
                for m in tar:
                    if m.name not in remaining:
                        continue
                    row = by_member[(archive, m.name)]
                    futures[pool.submit(work, row, tar.extractfile(m).read())] = row
                    remaining.discard(m.name)
                    if not remaining:
                        break
            for fut in concurrent.futures.as_completed(futures):
                row = futures[fut]
                try:
                    dest, size, md5, note = fut.result()
                    st.db.execute("UPDATE uploads SET status='staged', staged_path=?, staged_size=?, staged_md5=?, "
                                  "exif_note=?, updated_at=? WHERE upload_id=?",
                                  (dest, size, md5, note, now(), row["upload_id"]))
                    done += 1
                except PairingError as e:
                    st.db.execute("UPDATE uploads SET status='needs_pairing', last_error=?, updated_at=? "
                                  "WHERE upload_id=?", (str(e), now(), row["upload_id"]))
                    failed += 1
                except Exception as e:
                    st.db.execute("UPDATE uploads SET status='failed', last_error=?, updated_at=? WHERE upload_id=?",
                                  (f"stage: {e!r}"[:500], now(), row["upload_id"]))
                    failed += 1
            st.db.commit()
            log.info("staged from %s: %d ok, %d failed so far", archive, done, failed)
    st.event("stage", staged=done, failed=failed)
    return {"staged": done, "failed": failed}


# ---------------------------------------------------------------------- upload

class Stop:
    requested = False


class PairingError(Exception):
    """A motion clip whose own timestamp doesn't fit its still."""


def find_in_album(client, album_key, filename, md5=None):
    """Return (item_id, item_ref) of an item named `filename` (and, if given, with this md5) in the album."""
    for it in client.list_album_items(album_key):
        if (it["name"] or "").lower() == filename.lower() and (md5 is None or it["md5"] == md5):
            return it["item_id"], it["item_ref"]
    return None


def reconcile_uploads(st, client, rows):
    """For uploads with an unknown outcome, look in the target album before anything is retried."""
    fixed = {"done": 0, "staged": 0}
    for r in rows:
        album_key = st.one("SELECT album_key FROM targets WHERE name=?", r["target_name"])
        hit = find_in_album(client, album_key, r["upload_name"],
                            r["staged_md5"] if r["role"] == "still" else None) if album_key else None
        if hit:
            st.db.execute("UPDATE uploads SET status='done', image_key=?, album_image_uri=?, last_error=NULL, "
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
    if stuck:
        log.info("reconciling %d uploads with unknown outcome", len(stuck))
        reconcile_uploads(st, client, stuck)
    rows = select(st, ("staged",), args)
    if not rows:
        return {"uploaded": 0}

    def handler(sig, frame):
        if Stop.requested:
            raise KeyboardInterrupt
        Stop.requested = True
        log.warning("stop requested; finishing in-flight uploads (Ctrl-C again to abort)")
    signal.signal(signal.SIGINT, handler)

    folder_uri = client.ensure_folder_path(client.root_folder(), cfg["target_folder"])
    albums = {}
    for name in sorted({r["target_name"] for r in rows}):
        key = ensure_target(st, cfg, client, folder_uri, name)
        st.db.commit()
        albums[name] = key
    log.info("uploading %d files into %d albums", len(rows), len(albums))

    def one(r):
        ext = os.path.splitext(r["upload_name"])[1].upper()
        return client.upload_file(albums[r["target_name"]], r["staged_path"], r["upload_name"], CONTENT_TYPES[ext])

    totals = {"uploaded": 0, "failed": 0, "unknown": 0}
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        pending = iter(rows)
        inflight = {}

        def submit_next():
            r = next(pending, None)
            if r is None or Stop.requested:
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
                st.db.execute("UPDATE uploads SET status='done', image_key=?, album_image_uri=?, last_error=NULL, "
                              "updated_at=? WHERE upload_id=?", (item["item_id"], item["item_ref"], now(), r["upload_id"]))
                totals["uploaded"] += 1
            except SmugMugError as e:
                status = "unknown" if e.ambiguous else "failed"
                st.db.execute("UPDATE uploads SET status=?, last_error=?, updated_at=? WHERE upload_id=?",
                              (status, str(e)[:500], now(), r["upload_id"]))
                totals[status] += 1
                st.event("upload_error", level="warning", commit=False, upload_id=r["upload_id"], error=str(e))
            st.db.commit()
            if (totals["uploaded"] + totals["failed"]) % 100 == 0:
                log.info("uploaded %d, failed %d, unknown %d (remaining %d)", totals["uploaded"], totals["failed"],
                         totals["unknown"], len(rows) - sum(totals.values()))
            submit_next()
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
            st.event("album_sort_failed", level="warning", album_key=key, name=name, error=str(e))
    st.event("upload", **totals)
    return totals


# ---------------------------------------------------------------------- remove

def cmd_remove(st, cfg, client, args):
    """Delete specific uploads this tool made (by upload id) from SmugMug. Dry run unless --yes."""
    ids = [int(x) for x in args.ids]
    rows = [dict(r) for r in st.q(f"SELECT * FROM uploads WHERE upload_id IN ({','.join('?' * len(ids))})", *ids)]
    out = {"removed": 0, "skipped": 0}
    for r in rows:
        if r["status"] != "done" or not r["album_image_uri"]:
            log.warning("upload %s (%s) is %s; not ours to remove", r["upload_id"], r["upload_name"], r["status"])
            out["skipped"] += 1
            continue
        if not args.yes:
            log.info("dry run: would remove %s from %s", r["upload_name"], r["target_name"])
            continue
        try:
            client.remove_item(r["album_image_uri"])
        except NotFound:
            log.info("%s already gone", r["upload_name"])
        new_status = "needs_pairing" if r["role"] == "clip" else "staged"
        st.db.execute("UPDATE uploads SET status=?, image_key=NULL, album_image_uri=NULL, last_error=?, updated_at=? "
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
    rows = st.q(f"SELECT upload_id, target_name, image_key FROM uploads WHERE {where}")
    by_album = {}
    for r in rows:
        if not args.target or any(fnmatch.fnmatch(r["target_name"], p) for p in args.target):
            by_album.setdefault(r["target_name"], []).append((r["upload_id"], r["image_key"]))
    log.info("verifying %d uploads in %d albums", sum(len(v) for v in by_album.values()), len(by_album))

    album_keys = {name: st.one("SELECT album_key FROM targets WHERE name=?", name) for name in by_album}

    def listing(name):  # runs in a worker thread: no database access here
        return name, {it["item_id"] for it in client.list_album_items(album_keys[name], ids_only=True)}

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
                log.error("%s: %d uploaded images missing on server: %s", name, len(missing), sorted(missing)[:10])
                st.event("upload_verify_mismatch", level="error", commit=False, name=name, missing=sorted(missing))
            else:
                out["albums_ok"] += 1
            st.db.commit()
            log.info("verify %d/%d: %s (%d ok%s)", n, len(by_album), name, len(ok),
                     f", {len(missing)} MISSING" if missing else "")
    return out


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
    p.add_argument("--config", default="data/consolidate.json")
    p.add_argument("--index", default="data/takeout_index.db")
    p.add_argument("--takeout-dir", default="data/takeout")
    p.add_argument("--stage-dir", default="data/stage")
    p.add_argument("command", choices=["plan", "stage", "upload", "verify", "report", "remove"])
    p.add_argument("ids", nargs="*", help="remove: upload ids")
    p.add_argument("--yes", action="store_true", help="remove: actually delete (default dry run)")
    p.add_argument("--reason", default="misplaced", help="remove: recorded reason")
    p.add_argument("--all", action="store_true", help="verify: re-check everything, not just unverified uploads")
    p.add_argument("--target", action="append", help="only these target albums (glob; repeatable)")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg["log_file"])
    st = State(cfg["state_db"])
    client = (SmugMugClient.from_config_file(cfg["smugmug_config"])
              if args.command in ("upload", "verify", "remove") else None)
    st.start_run(f"takeout_upload.{args.command}", vars(args))
    try:
        if args.command == "plan":
            summary = cmd_plan(st, cfg, args)
        elif args.command == "stage":
            summary = cmd_stage(st, cfg, args)
        elif args.command == "upload":
            summary = cmd_upload(st, cfg, client, args)
        elif args.command == "verify":
            summary = cmd_verify(st, cfg, client, args)
        elif args.command == "remove":
            summary = cmd_remove(st, cfg, client, args)
        else:
            summary = cmd_report(st, cfg, args)
    except BaseException as e:
        st.db.rollback()
        st.event("run_failed", level="error", error=repr(e))
        st.finish_run("failed", {"error": repr(e)})
        raise
    st.finish_run("stopped" if Stop.requested else "ok", summary)
    if summary is not None:
        print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
