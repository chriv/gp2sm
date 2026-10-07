"""`gp2sm consolidate apply` end to end in a project: dry run by default, --yes moves, lock released."""

import os

import pytest

from gp2sm.organize import consolidate
from gp2sm.project import context
from gp2sm.project.init import main as init_main
from gp2sm.state import State
from tests.fakes.smugmug import FakeSmugMug


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    root = tmp_path / "proj"
    init_main([str(root), "--non-interactive", "--name", "T", "--timezone", "UTC", "--folder", "Root"],
              show=lambda *a: None)
    s = State(str(root / "state.db"))
    s.db.execute("INSERT INTO targets(name, kind, album_key) VALUES('T', 'photo', 'DST')")
    for k in "abc":
        s.db.execute("INSERT INTO images(image_key, serial, src_album_key, current_album_key) VALUES(?,0,'SRC','SRC')",
                     (k,))
        s.db.execute("INSERT INTO plan(image_key, action, target_name, status) VALUES(?,'move','T','pending')", (k,))
    s.db.commit()
    s.db.close()
    fake = FakeSmugMug(albums={"SRC": set("abc"), "DST": set()})
    monkeypatch.setattr(context, "client", lambda cfg: fake)
    return root, fake


def statuses(root):
    s = State(str(root / "state.db"))
    try:
        return dict(s.q("SELECT image_key, status FROM plan")), s.one("SELECT status FROM runs ORDER BY run_id DESC")
    finally:
        s.db.close()


def test_apply_is_a_dry_run_without_yes(project, capsys):
    root, fake = project
    assert consolidate.main(["--project", str(root), "apply"]) == 0
    out = capsys.readouterr().out
    assert "would move     3 -> T" in out and "Dry run" in out
    assert fake.albums["DST"] == set()
    plan, run_status = statuses(root)
    assert set(plan.values()) == {"pending"} and run_status == "ok"
    assert not os.path.exists(root / ".gp2sm.lock")


def test_apply_with_yes_moves_and_releases_lock(project):
    root, fake = project
    assert consolidate.main(["--project", str(root), "apply", "--yes"]) == 0
    assert fake.albums["DST"] == set("abc")
    plan, run_status = statuses(root)
    assert set(plan.values()) == {"done"} and run_status == "ok"
    assert not os.path.exists(root / ".gp2sm.lock")


def test_apply_refuses_while_another_run_holds_the_lock(project):
    root, fake = project
    (root / ".gp2sm.lock").write_text(f"{os.getppid()} consolidate apply")
    with pytest.raises(context.Locked):
        consolidate.main(["--project", str(root), "apply", "--yes"])
    assert fake.albums["DST"] == set()
