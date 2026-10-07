import argparse
import json
import os

import pytest

from gp2sm.cli import status
from gp2sm.project import context, credentials
from gp2sm.project.init import main as init_main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    return tmp_path


def make_project(root, credentials_name="smugmug"):
    init_main([str(root), "--non-interactive", "--name", "Test", "--timezone", "UTC", "--folder", "Photos",
               "--credentials", credentials_name], show=lambda *a: None)
    return root


def parse(argv, paths=("index", "takeout_dir", "stage_dir", "state")):
    p = argparse.ArgumentParser()
    context.add_args(p, paths=paths)
    return p.parse_args(argv)


def test_explicit_project_fills_paths_and_settings(store):
    root = make_project(store / "proj")
    args = parse(["--project", str(root)])
    cfg = context.resolve(args)
    assert args.project_root == str(root)
    assert cfg["state_db"] == str(root / "state.db") == args.state
    assert args.index == str(root / "takeout_index.db")
    assert args.takeout_dir == str(root / "takeout")
    assert args.stage_dir == str(root / "stage")
    assert cfg["target_folder"] == "Photos"
    assert cfg["smugmug_config"] is None  # no credentials stored yet
    assert os.path.isdir(root / "logs")


def test_discovers_project_upward(store, monkeypatch):
    root = make_project(store / "proj")
    (root / "takeout" / "deep").mkdir()
    monkeypatch.chdir(root / "takeout" / "deep")
    args = parse([])
    context.resolve(args)
    assert os.path.realpath(args.project_root) == os.path.realpath(root)


def test_explicit_paths_win(store):
    root = make_project(store / "proj")
    args = parse(["--project", str(root), "--index", "elsewhere.db"])
    context.resolve(args)
    assert args.index == "elsewhere.db"


def test_credentials_come_from_store(store):
    root = make_project(store / "proj", credentials_name="mine")
    credentials.save("mine", {"api_key": "k", "api_secret": "s", "oauth_token": "t", "oauth_token_secret": "ts"})
    cfg = context.resolve(parse(["--project", str(root)]))
    assert cfg["smugmug_config"] == credentials.path("mine")
    assert context.client(cfg) is not None


def test_missing_credentials_error_names_auth_command(store):
    root = make_project(store / "proj", credentials_name="mine")
    cfg = context.resolve(parse(["--project", str(root)]))
    with pytest.raises(context.NoProject, match="gp2sm auth smugmug --name mine"):
        context.client(cfg)


def test_legacy_json_config(store, monkeypatch):
    monkeypatch.chdir(store)
    (store / "data").mkdir()
    (store / "data" / "consolidate.json").write_text(json.dumps({"state_db": "data/x.db"}))
    args = parse([])
    cfg = context.resolve(args)
    assert args.project_root is None
    assert cfg["state_db"] == "data/x.db" == args.state
    assert args.index == "data/takeout_index.db"


def test_no_project_suggests_init(store, monkeypatch):
    monkeypatch.chdir(store)
    with pytest.raises(context.NoProject, match="gp2sm init"):
        context.resolve(parse([]))


def test_lock_excludes_second_holder_and_recovers_stale(tmp_path):
    db = str(tmp_path / "state.db")
    path = context.acquire_lock(db, "first")
    try:
        with pytest.raises(context.Locked, match="first"):
            context.acquire_lock(db, "second")
    finally:
        os.remove(path)
    with open(path, "w") as f:
        f.write("999999999 crashed run")  # no such process
    assert context.acquire_lock(db, "third") == path
    assert open(path).read().endswith("third")
    os.remove(path)


def test_status_fresh_project(store):
    root = make_project(store / "proj")
    lines = []
    assert status.main(["--project", str(root)], show=lines.append) == 0
    text = "\n".join(lines)
    assert "MISSING" in text and "No state yet" in text


def test_status_summarizes_state(store):
    from gp2sm.state import State
    root = make_project(store / "proj")
    st = State(str(root / "state.db"))
    st.start_run("plan", {})
    st.db.close()
    lines = []
    status.main(["--project", str(root)], show=lines.append)
    text = "\n".join(lines)
    assert "schema version" in text and "plan" in text


def test_release_lock_only_removes_own_lock(tmp_path):
    db = str(tmp_path / "state.db")
    path = context.acquire_lock(db, "mine")
    context.release_lock(path)
    assert not os.path.exists(path)
    with open(path, "w") as f:
        f.write(f"{os.getppid()} someone else")
    context.release_lock(path)
    assert os.path.exists(path)
    context.release_lock(None)
