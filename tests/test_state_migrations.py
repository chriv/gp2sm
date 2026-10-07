import sqlite3

from gp2sm.state import State, migrations

V1_UPLOADS = """CREATE TABLE uploads(upload_id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, role TEXT, archive TEXT,
  src_path TEXT, upload_name TEXT, target_name TEXT, reason TEXT, staged_path TEXT, staged_size INT, staged_md5 TEXT,
  exif_note TEXT, status TEXT, image_key TEXT, album_image_uri TEXT, attempts INT DEFAULT 0, last_error TEXT,
  updated_at TEXT, UNIQUE(item_id, role))"""


def make_v1(path):
    db = sqlite3.connect(path)
    db.executescript(f"""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('schema_version', '1');
        CREATE TABLE images(image_key TEXT PRIMARY KEY, filename TEXT);
        {V1_UPLOADS};
        INSERT INTO uploads(item_id, role, upload_name, status) VALUES(7, 'clip', 'IMG_1.MP4', 'done');
    """)
    db.commit()
    db.close()


def cols(st, table):
    return {r[1] for r in st.db.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_is_created_at_latest(tmp_path):
    st = State(str(tmp_path / "new.db"))
    assert st.schema_version == migrations.LATEST and st.migrations_applied == []
    assert {"pair_item_id", "verified_at"} <= cols(st, "uploads")
    assert st.q("SELECT version FROM schema_history")[0][0] == migrations.LATEST


def test_v1_db_upgrades_in_place_and_keeps_data(tmp_path):
    path = str(tmp_path / "old.db")
    make_v1(path)
    st = State(path)
    assert st.migrations_applied == [2] and st.schema_version == 2
    assert {"pair_item_id", "verified_at"} <= cols(st, "uploads")
    assert st.one("SELECT upload_name FROM uploads WHERE item_id=7") == "IMG_1.MP4"   # data preserved
    assert [r[0] for r in st.q("SELECT version FROM schema_history")] == [2]


def test_reopen_is_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    make_v1(path)
    State(path)
    st = State(path)
    assert st.migrations_applied == [] and st.schema_version == migrations.LATEST


def test_step_is_idempotent_when_columns_already_exist(tmp_path):
    path = str(tmp_path / "patched.db")
    make_v1(path)
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE uploads ADD COLUMN pair_item_id INT")   # what the old stopgap already did
    db.commit()
    db.close()
    assert State(path).schema_version == 2
