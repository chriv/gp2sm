"""Index Google Takeout archives (.tgz/.tar.gz/.zip-less tar streams) without extracting them.

One streaming pass per archive records every member's path, size and MD5, and stores the full
text of every .json sidecar. Archives are processed in parallel; each archive is committed only
when fully read, so an interrupted run simply re-reads that archive.

Usage: gp2sm takeout-index data/takeout/*.tgz [--db data/takeout_index.db]
"""

import argparse
import concurrent.futures
import hashlib
import logging
import os
import sqlite3
import sys
import tarfile
import time

log = logging.getLogger("gp2sm.takeout.index")

SCHEMA = """
CREATE TABLE IF NOT EXISTS archives(name TEXT PRIMARY KEY, size INT, members INT, indexed_at TEXT, seconds REAL);
CREATE TABLE IF NOT EXISTS members(
  archive TEXT, path TEXT, size INT, mtime INT, md5 TEXT, ext TEXT, folder TEXT, basename TEXT,
  json TEXT, PRIMARY KEY(archive, path));
CREATE INDEX IF NOT EXISTS members_md5 ON members(md5);
CREATE INDEX IF NOT EXISTS members_folder ON members(folder);
"""
CHUNK = 8 * 1024 * 1024


def index_archive(path):
    """Stream one archive; return (archive_name, rows, seconds)."""
    name = os.path.basename(path)
    t0 = time.time()
    rows = []
    with tarfile.open(path, mode="r|*") as tar:
        for m in tar:
            if not m.isfile():
                continue
            f = tar.extractfile(m)
            md5 = hashlib.md5()
            js = None
            is_json = m.name.lower().endswith(".json")
            buf = bytearray() if is_json else None
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                md5.update(chunk)
                if is_json:
                    buf.extend(chunk)
            if is_json:
                js = buf.decode("utf-8", errors="replace")
            folder, base = os.path.split(m.name)
            ext = os.path.splitext(base)[1].lower()
            rows.append((name, m.name, m.size, int(m.mtime), md5.hexdigest(), ext, folder, base, js))
            if len(rows) % 5000 == 0:
                log.info("%s: %d members (%.0fs)", name, len(rows), time.time() - t0)
    return name, rows, time.time() - t0


def main(argv=None):
    p = argparse.ArgumentParser(prog="gp2sm takeout-index", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archives", nargs="+")
    p.add_argument("--db", default="data/takeout_index.db")
    p.add_argument("--force", action="store_true", help="re-index archives already indexed")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
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
                db.executemany("INSERT INTO members VALUES(?,?,?,?,?,?,?,?,?)", rows)
                db.execute("INSERT OR REPLACE INTO archives VALUES(?,?,?,datetime('now'),?)",
                           (name, os.path.getsize(path), len(rows), secs))
            log.info("DONE %s: %d members in %.0fs", name, len(rows), secs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
