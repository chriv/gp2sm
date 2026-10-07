"""SQLite state for consolidation. Single source of truth; every change is also written to `events`."""

import datetime
import json
import sqlite3

from gp2sm.state import migrations

# TODO(schema, ROADMAP A1): this schema is SmugMug- and Google-shaped. That's acceptable while each
# database tracks one job, but long-term tracking needs a neutral, versioned schema with a
# backward-compatible migration. Specifically:
#   1. Rename service-shaped columns to neutral names, and add a `service` column wherever an id is stored:
#        images.image_key -> item_id, images.serial -> (fold into item_ref),
#        images.src_album_key/current_album_key -> src_collection_id/current_collection_id,
#        images.src_album_image_uri -> src_item_ref, images.archived_md5/archived_size -> stored_md5/stored_size,
#        images.capture_dt_smug -> capture_time_dest, images.raw_image/raw_metadata -> raw_item/raw_meta (JSON);
#        source_albums -> source_collections(collection_id, ref, name, path, item_count, ...);
#        targets.album_key/album_uri/node_uri -> collection_id/collection_ref/parent_ref;
#        plan.image_key, dup_groups.image_key/keeper_image_key, matches.image_key -> item_id/keeper_item_id;
#        uploads.image_key/album_image_uri -> dest_item_id/dest_item_ref.
#   2. Source-side ids: matches.google_id and legacy_items/legacy_md5 belong to a Google "legacy bridge"
#      plugin. Move them to plugin-owned tables (source_items(service, source_id, ...)) keyed by service.
#   3. Migrations are versioned (state/migrations.py); the neutral renames above become the next step(s)
#      there, each tested against a synthetic DB of the previous version.
#   4. Keep `events` append-only and service-neutral (`detail` JSON already is), so history stays readable
#      across migrations.

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
  image_count INT, rows_stored INT, inventoried_at TEXT,
  folder TEXT);  -- display-name folder path (organize rules match "folder/name")

-- One row per SmugMug image found in a source album.
CREATE TABLE IF NOT EXISTS images(
  image_key TEXT PRIMARY KEY, serial INT,
  src_album_key TEXT, src_album_image_uri TEXT,
  current_album_key TEXT,
  filename TEXT, format TEXT, is_video INT,
  archived_md5 TEXT, archived_size INT, width INT, height INT, duration_s REAL,
  uploaded TEXT, capture_dt_smug TEXT,
  raw_image TEXT, raw_metadata TEXT,
  first_seen TEXT, last_seen TEXT,
  make TEXT, model TEXT);  -- camera, from the destination's metadata (organize rules)

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
  pair_item_id INT,  -- for a re-paired clip: the Takeout item of the still it belongs next to
  verified_at TEXT,  -- when verify last confirmed this upload on the server
  source_ref TEXT,   -- the source's own reference for the file (importer rows; item_id is the legacy identity)
  convert INT,       -- importer rows: convert to JPEG while staging
  content_type TEXT, -- importer rows: upload content type
  taken_ts INT,      -- importer rows: capture time (fills a missing EXIF date when converting)
  pair_ref TEXT,     -- importer rows: for a clip, the source ref of the still it sits beside
  UNIQUE(item_id, role));
CREATE INDEX IF NOT EXISTS uploads_status ON uploads(status, target_name);

-- Unsorted clips placed beside their still by capture time + aspect ratio. Because the destination may not
-- support renames, placing means: upload the clip again under the still's name in the still's album,
-- verify it, then remove the old copy (that removal is gated by --yes).
-- status: planned -> uploaded -> verified -> done ; failed ; unknown
-- Items in an undated album that got a date from evidence, and their move to a dated album.
-- kind: image (from the consolidation `plan`) | upload (from `uploads`).
-- status: planned -> done ; unresolved ; failed
CREATE TABLE IF NOT EXISTS datings(
  dating_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, ref_id TEXT, item_id TEXT, serial INT, name TEXT, is_video INT, role TEXT,
  from_album TEXT, method TEXT, capture_ts INT, capture_local TEXT, target_name TEXT, evidence TEXT,
  status TEXT, last_error TEXT, updated_at TEXT, UNIQUE(kind, ref_id));

-- Importer (service-neutral): what is already on the destination, and whether each source item is there.
-- dest_* is a snapshot of the albums in scope (refreshed by `inventory`).
CREATE TABLE IF NOT EXISTS dest_albums(album_id TEXT PRIMARY KEY, name TEXT, folder TEXT, item_count INT,
  listed_at TEXT);
CREATE TABLE IF NOT EXISTS dest_items(item_id TEXT, album_id TEXT, serial INT, name TEXT, md5 TEXT, size INT,
  width INT, height INT, is_video INT, duration_s REAL, PRIMARY KEY(item_id, album_id));
CREATE INDEX IF NOT EXISTS dest_items_md5 ON dest_items(md5);
-- Cached perceptual hashes: source items (4 rotations) and destination items.
CREATE TABLE IF NOT EXISTS hash_source(source_ref TEXT PRIMARY KEY, h0 TEXT, h1 TEXT, h2 TEXT, h3 TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS hash_dest(item_id TEXT PRIMARY KEY, h TEXT, error TEXT);
-- decision: exact (same bytes) | same (same picture/video) | new | review ; reviewed: a person's same|different
CREATE TABLE IF NOT EXISTS source_matches(source_ref TEXT PRIMARY KEY, decision TEXT, dest_item_id TEXT,
  dest_album_id TEXT, dist INT, method TEXT, detail TEXT, decided_at TEXT, reviewed TEXT);

-- Album management (gp2sm albums): albums in scope as last seen, with sampled photo dates for albums whose
-- names carry no date; and every planned or applied album change with the value it replaced (for undo).
-- album_changes.status: planned -> done | failed ; review (needs a person's approve) ; undone ; skipped
CREATE TABLE IF NOT EXISTS albums_seen(album_id TEXT PRIMARY KEY, name TEXT, folder TEXT, item_count INT,
  photo_dates TEXT, listed_at TEXT);
CREATE TABLE IF NOT EXISTS album_changes(
  change_id INTEGER PRIMARY KEY AUTOINCREMENT, album_id TEXT, kind TEXT, field TEXT,
  old_value TEXT, new_value TEXT, confidence TEXT, source TEXT, note TEXT,
  status TEXT, planned_at TEXT, applied_at TEXT, last_error TEXT);
CREATE INDEX IF NOT EXISTS album_changes_album ON album_changes(album_id, field, status);

CREATE TABLE IF NOT EXISTS placements(
  placement_id INTEGER PRIMARY KEY AUTOINCREMENT,
  upload_id INT UNIQUE,                       -- the clip's row in uploads
  old_target TEXT, old_item_id TEXT, old_item_ref TEXT, old_name TEXT,
  still_name TEXT, new_target TEXT, new_name TEXT, seconds_apart INT, candidates INT,
  source_path TEXT, source_kind TEXT,          -- staged | takeout | destination
  status TEXT, new_item_id TEXT, new_item_ref TEXT, last_error TEXT, updated_at TEXT);
"""


# Indexes on columns that migrations add: run after migrating, so existing DBs have the columns first.
POST_MIGRATION = """
CREATE UNIQUE INDEX IF NOT EXISTS uploads_source ON uploads(source_ref, role) WHERE source_ref IS NOT NULL;
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
        fresh = not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='images'").fetchone()
        self.db.executescript(SCHEMA)
        self.migrations_applied = migrations.migrate(self.db, fresh)
        self.db.executescript(POST_MIGRATION)
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
