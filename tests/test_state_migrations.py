"""State schema versions: fresh databases, the migration machinery, refusal of pre-release and newer DBs."""

import sqlite3

import pytest

from gp2sm.state import PreReleaseState, State, migrations


def cols(st, table):
    return {r[1] for r in st.db.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_is_created_at_latest(tmp_path):
    st = State(str(tmp_path / "new.db"))
    assert st.schema_version == migrations.latest() == 1 and st.migrations_applied == []
    assert {"item_id", "keeper_item_id"} <= cols(st, "plan") and "image_key" not in cols(st, "plan")
    assert {"source_ref", "item_id", "item_ref"} <= cols(st, "uploads")
    assert st.q("SELECT version FROM schema_history")[0][0] == 1


def test_pre_release_databases_are_refused(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE images(image_key TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    with pytest.raises(PreReleaseState, match="pre-release gp2sm"):
        State(str(path))


def test_migrations_run_in_order_once(tmp_path, monkeypatch):
    path = str(tmp_path / "s.db")
    State(path).db.close()
    calls = []

    def step2(db):
        calls.append(2)
        if "note" not in {r[1] for r in db.execute("PRAGMA table_info(plan)")}:
            db.execute("ALTER TABLE plan ADD COLUMN note TEXT")
    monkeypatch.setattr(migrations, "MIGRATIONS", [(2, "plan.note", step2)])
    st = State(path)
    assert st.migrations_applied == [2] and st.schema_version == 2 and "note" in cols(st, "plan")
    st.db.close()
    st = State(path)                                     # reopening applies nothing
    assert st.migrations_applied == [] and calls == [2]
    assert [r[0] for r in st.q("SELECT version FROM schema_history ORDER BY 1")] == [1, 2]


def test_newer_database_is_refused(tmp_path):
    path = str(tmp_path / "s.db")
    st = State(path)
    st.db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    st.db.commit()
    st.db.close()
    with pytest.raises(SystemExit, match="upgrade gp2sm"):
        State(path)
