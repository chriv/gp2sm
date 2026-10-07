import argparse

import pytest

from gp2sm.organize import date_undated
from gp2sm.state import State
from tests.fakes.smugmug import FakeSmugMug

CFG = {"target_folder": "Root", "undated_photo_album": "Undated", "undated_video_album": "Videos Undated",
       "photo_album_template": "P {yyyy}-{mm}", "video_album_template": "P {yyyy}-{mm}", "timezone": "UTC"}


@pytest.fixture
def st(tmp_path):
    s = State(str(tmp_path / "s.db"))
    s.start_run("test", {})
    s.db.execute("INSERT INTO targets(name, kind, album_key, created_at) VALUES('Undated', 'photo_undated', 'A_Undated', 'x')")
    for key in ("k1", "k2"):
        s.db.execute("INSERT INTO plan(image_key, action, target_name, status) VALUES(?, 'move', 'Undated', 'done')", (key,))
        s.db.execute("INSERT INTO images(image_key, serial, current_album_key) VALUES(?, 0, 'A_Undated')", (key,))
        s.db.execute("INSERT INTO matches(image_key) VALUES(?)", (key,))
        s.db.execute("INSERT INTO datings(kind, ref_id, item_id, serial, name, is_video, role, from_album, method, "
                     "capture_ts, capture_local, target_name, status) VALUES('image', ?, ?, 0, ?, 0, 'still', "
                     "'Undated', 'name_month', 1688479200, '2023-07-04T14:00:00', 'P 2023-07', 'planned')",
                     (key, key, key + ".jpg"))
    s.db.commit()
    return s


def test_move_is_confirmed_on_server_and_silent_noops_fail(st):
    fake = FakeSmugMug(albums={"A_Undated": {"k1", "k2"}}, processing={"k2"})
    out = date_undated.cmd_apply(st, CFG, fake, argparse.Namespace(yes=True))
    assert out == {"moved": 1, "failed": 1}
    status = dict(st.q("SELECT ref_id, status FROM datings"))
    assert status == {"k1": "done", "k2": "failed"}            # the ignored move is not recorded as done
    assert fake.albums["A_P202307"] == {"k1"} and "k2" in fake.albums["A_Undated"]
    assert st.one("SELECT target_name FROM plan WHERE image_key='k1'") == "P 2023-07"


def test_apply_without_yes_is_a_dry_run(st, capsys):
    fake = FakeSmugMug(albums={"A_Undated": {"k1", "k2"}})
    assert date_undated.cmd_apply(st, CFG, fake, argparse.Namespace(yes=False)) is None
    out = capsys.readouterr().out
    assert "would move k1.jpg -> P 2023-07" in out and "Dry run" in out
    assert fake.albums == {"A_Undated": {"k1", "k2"}}
    assert dict(st.q("SELECT ref_id, status FROM datings")) == {"k1": "planned", "k2": "planned"}
