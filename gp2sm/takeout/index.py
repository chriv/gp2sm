"""Index Google Takeout archives (.zip or .tgz) without extracting them.

One streaming pass per archive records every member's path, size and MD5, stores the full text of
every .json sidecar, and reads each photo/video's own facts: dimensions, duration, and the capture time
the file itself records (camera EXIF, or the clip's Apple/MP4 date). Archives are processed in parallel;
each archive is committed only when fully read, so an interrupted run simply re-reads that archive.

Usage: gp2sm takeout index   (every archive in the project's takeout folder)
"""

import argparse
import concurrent.futures
import hashlib
import logging
import os
import sqlite3
import sys
import time

from gp2sm.media.mp4 import Partial
from gp2sm.media.probe import VIDEO_EXTS, is_media, probe
from gp2sm.takeout.archive import iter_members

log = logging.getLogger("gp2sm.takeout.index")

SCHEMA = """
CREATE TABLE IF NOT EXISTS archives(name TEXT PRIMARY KEY, size INT, members INT, indexed_at TEXT, seconds REAL);
CREATE TABLE IF NOT EXISTS members(
  archive TEXT, path TEXT, size INT, mtime INT, md5 TEXT, ext TEXT, folder TEXT, basename TEXT,
  json TEXT, width INT, height INT, duration_s REAL, own_time TEXT, probe_error TEXT, PRIMARY KEY(archive, path));
CREATE INDEX IF NOT EXISTS members_md5 ON members(md5);
CREATE INDEX IF NOT EXISTS members_folder ON members(folder);
"""
CHUNK = 8 * 1024 * 1024
FULL_READ_MAX = 64 * 1024 * 1024  # media up to this size is probed whole
EDGE = 16 * 1024 * 1024           # larger videos: probe their first and last 16 MB (where MP4 boxes live)
PROBE_COLUMNS = (("width", "INT"), ("height", "INT"), ("duration_s", "REAL"), ("own_time", "TEXT"),
                 ("probe_error", "TEXT"))


def upgrade(db):
    """Add the media-probe columns to an index made before they existed (left NULL until re-indexed)."""
    have = {r[1] for r in db.execute("PRAGMA table_info(members)")}
    for name, kind in PROBE_COLUMNS:
        if name not in have:
            db.execute(f"ALTER TABLE members ADD COLUMN {name} {kind}")


def probe_member(data, ext):
    """(width, height, duration_s, own_time, error) for a media member."""
    try:
        info = probe(data, ext)
    except Exception as e:  # an unreadable file is recorded, not fatal
        return None, None, None, None, f"{type(e).__name__}: {e}"[:300]
    return info["width"], info["height"], info["duration_s"], info["own_time"], None


def index_archive(path):
    """Stream one archive; return (archive_name, rows, seconds)."""
    name = os.path.basename(path)
    t0 = time.time()
    rows = []
    for m in iter_members(path):
        folder, base = os.path.split(m.name)
        ext = os.path.splitext(base)[1].lower()
        is_json = ext == ".json"
        media = is_media(ext)
        md5 = hashlib.md5()
        head = bytearray()
        tail = bytearray()
        whole = is_json or (media and (m.size <= FULL_READ_MAX or ext not in VIDEO_EXTS))
        with m.open() as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                md5.update(chunk)
                if whole:
                    head.extend(chunk)
                elif media:
                    if len(head) < EDGE:
                        head.extend(chunk[:EDGE - len(head)])
                    tail.extend(chunk)
                    del tail[:max(0, len(tail) - EDGE)]
        js = head.decode("utf-8", errors="replace") if is_json else None
        blob = bytes(head) if whole else Partial(head, tail, m.size)
        probed = probe_member(blob, ext) if media else (None,) * 5
        rows.append((name, m.name, m.size, m.mtime, md5.hexdigest(), ext, folder, base, js, *probed))
        if len(rows) % 5000 == 0:
            log.info("%s: %d members (%.0fs)", name, len(rows), time.time() - t0)
    return name, rows, time.time() - t0


def main(argv=None):
    """Index the given archives into --db (`gp2sm takeout index` calls this for a project's takeout folder)."""
    p = argparse.ArgumentParser(prog="gp2sm takeout index", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archives", nargs="+", help="Takeout .zip/.tgz files")
    p.add_argument("--db", required=True, help="index database")
    p.add_argument("--force", action="store_true", help="re-index archives already indexed")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    upgrade(db)
    done = {r[0] for r in db.execute("SELECT name FROM archives")}
    todo = [a for a in args.archives if args.force or os.path.basename(a) not in done]
    log.info("indexing %d archive(s): %s", len(todo), [os.path.basename(a) for a in todo])
    with concurrent.futures.ProcessPoolExecutor(max_workers=len(todo) or 1) as pool:
        futures = {pool.submit(index_archive, a): a for a in todo}
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            name, rows, secs = fut.result()
            with db:
                db.execute("DELETE FROM members WHERE archive=?", (name,))
                db.executemany("INSERT INTO members(archive, path, size, mtime, md5, ext, folder, basename, json, width, "
                               "height, duration_s, own_time, probe_error) VALUES(" + ",".join("?" * 14) + ")", rows)
                db.execute("INSERT OR REPLACE INTO archives VALUES(?,?,?,datetime('now'),?)",
                           (name, os.path.getsize(path), len(rows), secs))
            log.info("DONE %s: %d members in %.0fs", name, len(rows), secs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
