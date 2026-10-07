"""Ordered, versioned schema migrations for the state DB.

A new DB is created at LATEST directly (the SCHEMA already has every column). An existing DB gets each step
newer than its recorded version, in order, each in its own transaction, recorded in `schema_history`.
Steps must be idempotent (safe if a DB already has the change).

To add a migration: append (version, description, function) with the next version number, update SCHEMA
so new DBs get the change directly, and add a test that upgrades a synthetic DB of the previous version.
"""

import datetime


def _columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _m2_upload_pairing_and_verification(db):
    have = _columns(db, "uploads")
    for col, decl in (("pair_item_id", "INT"), ("verified_at", "TEXT")):
        if col not in have:
            db.execute(f"ALTER TABLE uploads ADD COLUMN {col} {decl}")


def _m3_importer(db):
    # dest_albums, dest_items, hash_source, hash_dest and source_matches come from SCHEMA (CREATE IF NOT EXISTS),
    # which runs before migrations; the uploads column has to be added here.
    have = _columns(db, "uploads")
    for col, decl in (("source_ref", "TEXT"), ("convert", "INT"), ("content_type", "TEXT"), ("taken_ts", "INT"),
                      ("pair_ref", "TEXT")):
        if col not in have:
            db.execute(f"ALTER TABLE uploads ADD COLUMN {col} {decl}")


MIGRATIONS = [
    (2, "uploads.pair_item_id (re-paired clips) and uploads.verified_at (incremental verify)",
     _m2_upload_pairing_and_verification),
    (3, "importer: destination inventory, hash caches, source_matches; uploads.source_ref/convert/"
        "content_type/taken_ts/pair_ref", _m3_importer),
]
LATEST = max(v for v, _, _ in MIGRATIONS)

HISTORY = "CREATE TABLE IF NOT EXISTS schema_history(version INT PRIMARY KEY, description TEXT, applied_at TEXT)"


def current_version(db):
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else 1


def migrate(db, fresh):
    """Bring the DB to LATEST. Returns the list of versions applied."""
    db.execute(HISTORY)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if fresh:
        db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(LATEST),))
        db.execute("INSERT OR IGNORE INTO schema_history VALUES(?, 'created at this version', ?)", (LATEST, stamp))
        db.commit()
        return []
    applied = []
    version = current_version(db)
    for target, description, step in MIGRATIONS:
        if target <= version:
            continue
        with db:  # one transaction per step
            step(db)
            db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(target),))
            db.execute("INSERT OR REPLACE INTO schema_history VALUES(?,?,?)", (target, description, stamp))
        applied.append(target)
        version = target
    return applied
