"""State schema versions: fresh databases, the migration machinery, refusal of pre-release and newer DBs."""

import sqlite3

import pytest

from gp2sm.state import PreReleaseState, State, migrations


def cols(st, table):
    return {r[1] for r in st.db.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_is_created_at_latest(tmp_path):
    st = State(str(tmp_path / "new.db"))
    assert st.schema_version == migrations.latest() == 2 and st.migrations_applied == []
    assert {"item_id", "keeper_item_id"} <= cols(st, "plan") and "image_key" not in cols(st, "plan")
    assert {"source_ref", "item_id", "item_ref"} <= cols(st, "uploads")
    assert {"target_name", "item_id"} <= cols(st, "target_items_before")
    assert st.q("SELECT version FROM schema_history")[0][0] == 2


def test_pre_release_databases_are_refused(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE images(image_key TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    with pytest.raises(PreReleaseState, match="pre-release gp2sm"):
        State(str(path))


def test_upgrade_from_version_1_adds_target_items_before(tmp_path):
    path = str(tmp_path / "v1.db")
    st = State(path)                                     # make it look like a version 1 database
    st.db.execute("DROP TABLE target_items_before")
    st.db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    st.db.execute("DELETE FROM schema_history")
    st.db.commit()
    st.db.close()
    st = State(path)
    assert st.migrations_applied == [2] and st.schema_version == 2
    assert {"target_name", "item_id"} <= cols(st, "target_items_before")


def test_migrations_run_in_order_once(tmp_path, monkeypatch):
    path = str(tmp_path / "s.db")
    State(path).db.close()
    calls = []

    def step2(db):
        calls.append(2)
        if "note" not in {r[1] for r in db.execute("PRAGMA table_info(plan)")}:
            db.execute("ALTER TABLE plan ADD COLUMN note TEXT")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [(3, "plan.note", step2)])
    st = State(path)
    assert st.migrations_applied == [3] and st.schema_version == 3 and "note" in cols(st, "plan")
    st.db.close()
    st = State(path)                                     # reopening applies nothing
    assert st.migrations_applied == [] and calls == [2]
    assert [r[0] for r in st.q("SELECT version FROM schema_history ORDER BY 1")] == [2, 3]


def test_newer_database_is_refused(tmp_path):
    path = str(tmp_path / "s.db")
    st = State(path)
    st.db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    st.db.commit()
    st.db.close()
    with pytest.raises(SystemExit, match="upgrade gp2sm"):
        State(path)
