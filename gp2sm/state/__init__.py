"""SQLite state for a project: the single source of truth; every change is also written to `events`.

Names are service-neutral: items and albums have the destination's own ids (`item_id`, `album_id`) and opaque
references (`item_ref`, `album_ref`); service details stay in the adapters. Schema versions are in
migrations.py; a database from a pre-release gp2sm (before the neutral schema) is refused, not converted.
"""

import datetime
import json
import sqlite3

from gp2sm.state import migrations

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT, args TEXT,
  started TEXT, finished TEXT, status TEXT, summary TEXT);

-- Append-only log of everything the tool did or observed.
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_id INT, level TEXT, kind TEXT,
  item_id TEXT, album_id TEXT, detail TEXT);

-- Organize (gp2sm organize): the source albums and their items, as inventoried.
CREATE TABLE IF NOT EXISTS source_albums(
  album_id TEXT PRIMARY KEY, album_ref TEXT, name TEXT, path TEXT, folder TEXT,
  item_count INT, rows_stored INT, inventoried_at TEXT);
CREATE TABLE IF NOT EXISTS items(
  item_id TEXT PRIMARY KEY, serial INT,
  src_album_id TEXT, src_item_ref TEXT, current_album_id TEXT,
  name TEXT, format TEXT, is_video INT, md5 TEXT, size INT, width INT, height INT, duration_s REAL,
  uploaded TEXT, capture_time TEXT, make TEXT, model TEXT,
  raw TEXT, raw_metadata TEXT, first_seen TEXT, last_seen TEXT);

-- Albums the plans put items into (organize and the Takeout importer).
CREATE TABLE IF NOT EXISTS targets(
  name TEXT PRIMARY KEY, kind TEXT, album_id TEXT, album_ref TEXT, node_ref TEXT,
  created_at TEXT, planned INT, server_count INT, checked_at TEXT);

-- What should happen to each organized item and how far it got.
-- action: move | collect | park_duplicate (keeper_item_id: the copy that is kept)
-- status: pending -> in_progress -> done | failed ; unknown = must be reconciled against the server ; deleted
CREATE TABLE IF NOT EXISTS plan(
  item_id TEXT PRIMARY KEY, action TEXT, target_name TEXT, reason TEXT, keeper_item_id TEXT,
  status TEXT, attempts INT DEFAULT 0, last_error TEXT, batch_id TEXT,
  planned_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS plan_status ON plan(status, target_name);

-- Files the importer uploads (gp2sm takeout plan/stage/upload/verify).
-- status: planned -> staged -> uploading -> done | failed | unknown ; needs_review ; skipped ; held
CREATE TABLE IF NOT EXISTS uploads(
  upload_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_ref TEXT, role TEXT, archive TEXT, src_path TEXT,
  upload_name TEXT, target_name TEXT, reason TEXT,
  convert INT, content_type TEXT, taken_ts INT, pair_ref TEXT,
  staged_path TEXT, staged_size INT, staged_md5 TEXT, exif_note TEXT,
  status TEXT, item_id TEXT, item_ref TEXT,
  attempts INT DEFAULT 0, last_error TEXT, updated_at TEXT, verified_at TEXT,
  UNIQUE(source_ref, role));
CREATE INDEX IF NOT EXISTS uploads_status ON uploads(status, target_name);

-- Importer: what is already on the destination (dest_*, refreshed by `inventory`), and whether each source
-- item is there. decision: exact (same bytes) | same (same picture/video) | new | review | source_duplicate ;
-- reviewed: a person's same | different
CREATE TABLE IF NOT EXISTS dest_albums(album_id TEXT PRIMARY KEY, name TEXT, folder TEXT, item_count INT,
  listed_at TEXT);
CREATE TABLE IF NOT EXISTS dest_items(item_id TEXT, album_id TEXT, serial INT, name TEXT, md5 TEXT, size INT,
  width INT, height INT, is_video INT, duration_s REAL, PRIMARY KEY(item_id, album_id));
CREATE INDEX IF NOT EXISTS dest_items_md5 ON dest_items(md5);
CREATE TABLE IF NOT EXISTS hash_source(source_ref TEXT PRIMARY KEY, h0 TEXT, h1 TEXT, h2 TEXT, h3 TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS hash_dest(item_id TEXT PRIMARY KEY, h TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS source_matches(source_ref TEXT PRIMARY KEY, decision TEXT, dest_item_id TEXT,
  dest_album_id TEXT, dist INT, method TEXT, detail TEXT, decided_at TEXT, reviewed TEXT);

-- Album management (gp2sm albums): albums in scope as last seen (with sampled photo dates for albums whose
-- names carry no date), and every planned or applied album change with the value it replaced (for undo).
-- album_changes.status: planned -> done | failed ; review (needs a person's approve) ; undone ; skipped
CREATE TABLE IF NOT EXISTS albums_seen(album_id TEXT PRIMARY KEY, name TEXT, folder TEXT, item_count INT,
  photo_dates TEXT, listed_at TEXT);
CREATE TABLE IF NOT EXISTS album_changes(
  change_id INTEGER PRIMARY KEY AUTOINCREMENT, album_id TEXT, kind TEXT, field TEXT,
  old_value TEXT, new_value TEXT, confidence TEXT, source TEXT, note TEXT,
  status TEXT, planned_at TEXT, applied_at TEXT, last_error TEXT);
CREATE INDEX IF NOT EXISTS album_changes_album ON album_changes(album_id, field, status);
"""

PRE_RELEASE_TABLES = ("images", "legacy_items", "dup_groups", "placements")


class PreReleaseState(SystemExit):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & set(PRE_RELEASE_TABLES):
            self.db.close()
            raise PreReleaseState(f"{path} was made by a pre-release gp2sm and can't be used: start a new project "
                                  "(`gp2sm init`), or move this file aside")
        fresh = "meta" not in tables
        self.db.executescript(SCHEMA)
        self.migrations_applied = migrations.migrate(self.db, fresh)
        self.run_id = None

    @property
    def schema_version(self):
        return migrations.current_version(self.db)

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

    def event(self, kind, *, level="info", item_id=None, album_id=None, commit=True, **detail):
        self.db.execute("INSERT INTO events(ts, run_id, level, kind, item_id, album_id, detail) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (now(), self.run_id, level, kind, item_id, album_id,
                         json.dumps(detail, default=str) if detail else None))
        if commit:
            self.db.commit()

    # ---------------------------------------------------------------- helpers

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return row[0] if row else None

    def set_plan_status(self, item_ids, status, *, error=None, batch_id=None, bump_attempts=False):
        self.db.executemany(
            "UPDATE plan SET status=?, last_error=?, batch_id=COALESCE(?, batch_id), updated_at=?, "
            "attempts=attempts+? WHERE item_id=?",
            [(status, error, batch_id, now(), 1 if bump_attempts else 0, k) for k in item_ids])
