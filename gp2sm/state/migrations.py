"""Ordered, versioned schema migrations for the state DB.

A new DB is created at the latest version directly (SCHEMA already has every column). An existing DB gets each step newer
than its recorded version, in order, each in its own transaction, recorded in `schema_history`. Steps must be
idempotent (safe if a DB already has the change).

To add a migration: append (version, description, function) with the next version number, update SCHEMA so
new DBs get the change directly, and add a test that upgrades a synthetic DB of the previous version.
Version 1 is the neutral schema of the first public release; earlier pre-release databases are refused
(state.PreReleaseState), not converted.
"""

import datetime


def _columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _v2_target_items_before(db):
    db.execute("CREATE TABLE IF NOT EXISTS target_items_before(target_name TEXT, item_id TEXT, "
               "PRIMARY KEY(target_name, item_id))")


MIGRATIONS = [   # (version, description, step(db)) for versions 2, 3, ...
    (2, "target_items_before: items already in a found target album", _v2_target_items_before),
]
BASE = 1

HISTORY = "CREATE TABLE IF NOT EXISTS schema_history(version INT PRIMARY KEY, description TEXT, applied_at TEXT)"


def latest():
    return max([BASE] + [v for v, _, _ in MIGRATIONS])


def current_version(db):
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return int(row[0]) if row else BASE


def migrate(db, fresh):
    """Bring the DB to the latest version. Returns the list of versions applied."""
    db.execute(HISTORY)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if fresh:
        db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(latest()),))
        db.execute("INSERT OR IGNORE INTO schema_history VALUES(?, 'created at this version', ?)", (latest(), stamp))
        db.commit()
        return []
    applied = []
    version = current_version(db)
    if version > latest():
        raise SystemExit(f"this state database is schema version {version}, newer than this gp2sm "
                         f"(knows up to {latest()}): upgrade gp2sm")
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
