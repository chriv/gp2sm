"""SQLite state for consolidation. Single source of truth; every change is also written to `events`."""

import datetime
import json
import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT, args TEXT,
  started TEXT, finished TEXT, status TEXT, summary TEXT);

-- Append-only log of everything the tool did or observed.
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id INT, level TEXT, kind TEXT,
  image_key TEXT, album_key TEXT, detail TEXT);

-- Albums whose contents are being consolidated.
CREATE TABLE IF NOT EXISTS source_albums(
  album_key TEXT PRIMARY KEY, album_uri TEXT, name TEXT, url_path TEXT,
  image_count INT, rows_stored INT, inventoried_at TEXT);

-- One row per SmugMug image found in a source album.
CREATE TABLE IF NOT EXISTS images(
  image_key TEXT PRIMARY KEY, serial INT,
  src_album_key TEXT, src_album_image_uri TEXT,
  current_album_key TEXT,
  filename TEXT, format TEXT, is_video INT,
  archived_md5 TEXT, archived_size INT, width INT, height INT, duration_s REAL,
  uploaded TEXT, capture_dt_smug TEXT,
  raw_image TEXT, raw_metadata TEXT,
  first_seen TEXT, last_seen TEXT);

-- Google items from the legacy transfer DBs (the bridge to capture dates and Google IDs).
CREATE TABLE IF NOT EXISTS legacy_items(
  google_id TEXT PRIMARY KEY, filename TEXT, mime_type TEXT, creation_ts TEXT,
  width INT, height INT, product_url TEXT, legacy_status TEXT, source_db TEXT);
CREATE TABLE IF NOT EXISTS legacy_md5(google_id TEXT, md5 TEXT, source_db TEXT, PRIMARY KEY(google_id, md5));
CREATE INDEX IF NOT EXISTS legacy_md5_md5 ON legacy_md5(md5);

-- How each SmugMug image was tied to a Google item (or why it wasn't).
CREATE TABLE IF NOT EXISTS matches(
  image_key TEXT PRIMARY KEY, google_id TEXT, method TEXT, confidence TEXT,
  capture_ts_utc TEXT, capture_local TEXT, candidates INT, notes TEXT);

-- Exact-duplicate groups (same ArchivedMD5) and the chosen keeper.
CREATE TABLE IF NOT EXISTS dup_groups(
  image_key TEXT PRIMARY KEY, group_key TEXT, group_size INT, keeper_image_key TEXT, is_keeper INT);

-- Destination albums.
CREATE TABLE IF NOT EXISTS targets(
  name TEXT PRIMARY KEY, kind TEXT, album_key TEXT, album_uri TEXT, node_uri TEXT,
  created_at TEXT, planned INT, server_count INT, checked_at TEXT);

-- What should happen to each image and how far it got.
-- status: pending -> in_progress -> done | failed ; 'unknown' = must be reconciled against the server
CREATE TABLE IF NOT EXISTS plan(
  image_key TEXT PRIMARY KEY, action TEXT, target_name TEXT, reason TEXT,
  status TEXT, attempts INT DEFAULT 0, last_error TEXT, batch_id TEXT,
  planned_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS plan_status ON plan(status, target_name);

-- Files uploaded from Google Takeout (Live Photo stills converted to JPEG, and motion clips).
-- status: planned -> staged -> uploading -> done | failed | unknown ; needs_review ; skipped
CREATE TABLE IF NOT EXISTS uploads(
  upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INT, role TEXT, archive TEXT, src_path TEXT,
  upload_name TEXT, target_name TEXT, reason TEXT,
  staged_path TEXT, staged_size INT, staged_md5 TEXT, exif_note TEXT,
  status TEXT, image_key TEXT, album_image_uri TEXT,
  attempts INT DEFAULT 0, last_error TEXT, updated_at TEXT,
  UNIQUE(item_id, role));
CREATE INDEX IF NOT EXISTS uploads_status ON uploads(status, target_name);
"""


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()
        self.run_id = None

    # ------------------------------------------------------------- runs/events

    def start_run(self, command, args):
        cur = self.db.execute("INSERT INTO runs(command, args, started, status) VALUES(?,?,?,?)",
                              (command, json.dumps(args, default=str), now(), "running"))
        self.db.commit()
        self.run_id = cur.lastrowid
        return self.run_id

    def finish_run(self, status, summary=None):
        self.db.execute("UPDATE runs SET finished=?, status=?, summary=? WHERE run_id=?",
                        (now(), status, json.dumps(summary, default=str) if summary else None, self.run_id))
        self.db.commit()

    def event(self, kind, *, level="info", image_key=None, album_key=None, commit=True, **detail):
        self.db.execute("INSERT INTO events(ts, run_id, level, kind, image_key, album_key, detail) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (now(), self.run_id, level, kind, image_key, album_key,
                         json.dumps(detail, default=str) if detail else None))
        if commit:
            self.db.commit()

    # ---------------------------------------------------------------- helpers

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return row[0] if row else None

    def set_plan_status(self, image_keys, status, *, error=None, batch_id=None, bump_attempts=False):
        self.db.executemany(
            "UPDATE plan SET status=?, last_error=?, batch_id=COALESCE(?, batch_id), updated_at=?, "
            "attempts=attempts+? WHERE image_key=?",
            [(status, error, batch_id, now(), 1 if bump_attempts else 0, k) for k in image_keys])
