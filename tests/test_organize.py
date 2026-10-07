"""Organize: the pure planner and `gp2sm organize` end to end against the fake destination."""

import os

import pytest

from gp2sm.organize import planning
from gp2sm.project import context
from gp2sm.project.init import main as init_main
from gp2sm.state import State
from tests.fakes.smugmug import FakeSmugMug

SETTINGS = {"photo": "{group} {yyyy}-{mm}", "video": "{group} {yyyy}-{mm}", "undated_photo": "{group} Undated",
            "undated_video": "{group} Videos Undated", "duplicates_album": "Duplicates", "duplicates": "park",
            "soft_cap": 4000}


def item(i, when=None, group="Phone", md5=None, uploaded="2024-01-01", video=False, method="camera"):
    return {"item_id": i, "filename": f"{i}.jpg", "is_video": video, "md5": md5 or f"m{i}", "uploaded": uploaded,
            "capture_local": when, "method": method if when else "none", "group": group}


def targets(rows):
    return {r["item_id"]: (r["action"], r["target_name"]) for r in rows}


def test_dated_grouped_undated_and_videos():
    rows = planning.plan_organize([item("a", "2023-05-04T10:00:00"), item("b", None), item("c", "2023-05-01", video=True),
                                   item("d", "2023-06-01", group="Kids")], SETTINGS)
    assert targets(rows) == {"a": ("move", "Phone 2023-05"), "b": ("move", "Phone Undated"),
                             "c": ("move", "Phone 2023-05"), "d": ("move", "Kids 2023-06")}
    assert next(r for r in rows if r["item_id"] == "a")["reason"] == "date via camera, group Phone"


def test_identical_copies_park_and_lend_their_date():
    rows = planning.plan_organize([item("k", None, md5="same", uploaded="2023-01-01"),
                                   item("x", "2022-12-25T08:00:00", md5="same", uploaded="2023-06-01")], SETTINGS)
    assert targets(rows) == {"k": ("move", "Phone 2022-12"), "x": ("park_duplicate", "Duplicates")}
    assert "from an identical copy" in next(r for r in rows if r["item_id"] == "k")["reason"]
    keep = planning.plan_organize([item("k", None, md5="same"), item("x", "2022-12-25", md5="same")],
                                  dict(SETTINGS, duplicates="keep"))
    assert {a for a, _ in targets(keep).values()} == {"move"}


def test_caps_split_and_done_items_stay():
    items = [item(f"i{n}", f"2023-05-{n + 1:02d}") for n in range(5)]
    rows = planning.plan_organize(items, dict(SETTINGS, soft_cap=2),
                                  existing={"i4": {"status": "done", "target_name": "Phone 2023-05"}})
    t = targets(rows)
    assert [t[f"i{n}"][1] for n in range(4)] == ["Phone 2023-05", "Phone 2023-05", "Phone 2023-05 - Part 2",
                                                 "Phone 2023-05 - Part 2"]
    assert t["i4"][1] == "Phone 2023-05"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    root = tmp_path / "proj"
    init_main([str(root), "--non-interactive", "--name", "T", "--timezone", "UTC", "--folder", "Organized"],
              show=lambda *a: None)
    with open(root / "gp2sm.toml", "a") as f:
        f.write('\n')
    text = (root / "gp2sm.toml").read_text()
    text = text.replace('photo = "Photos {yyyy}-{mm}"', 'photo = "{group} {yyyy}-{mm}"')
    text = text.replace('video = "Photos {yyyy}-{mm}"', 'video = "{group} {yyyy}-{mm}"')
    text = text.replace('undated_photo = "Photos Undated"', 'undated_photo = "{group} Undated"')
    text = text.replace('sources = []\n# where capture dates', 'sources = ["Uploads"]\n# where capture dates')
    text = text.replace('skip_newer_than_days = 0', 'skip_newer_than_days = 30')
    text = text.replace('group = []', 'group = [{name = "Phone", model = "iPhone*"}, {name = "Screens", filename = "Screen*"}]')
    (root / "gp2sm.toml").write_text(text)

    fake = FakeSmugMug()
    uploads = fake.ensure_folder_path(fake.root_folder(), "Uploads")
    src = fake.ensure_album(uploads, "Phone backup")[0]
    organized = fake.ensure_album(fake.ensure_folder_path(fake.root_folder(), "Organized"), "Phone 2020-01")[0]
    old = "2024-01-01T00:00:00+00:00"
    fake.items.update({
        "P1": {"name": "IMG_0001.JPG", "capture_time": "2023-05-04T10:00:00", "model": "iPhone 14", "md5": "a",
               "uploaded": old},
        "P2": {"name": "Screenshot 2023-06-01 at 09.00.00.png", "md5": "b", "uploaded": old},
        "P3": {"name": "IMG_0001.JPG", "capture_time": "2023-05-04T10:00:00", "model": "iPhone 14", "md5": "a",
               "uploaded": "2024-02-01T00:00:00+00:00"},                         # identical copy
        "P4": {"name": "mystery.jpg", "md5": "c", "uploaded": old},
        "P5": {"name": "IMG_9999.JPG", "capture_time": "2026-10-01T10:00:00", "model": "iPhone 16", "md5": "d",
               "uploaded": "2099-01-01T00:00:00+00:00"},                         # too recent (uploader still busy)
        "Q1": {"name": "already.jpg", "md5": "e", "uploaded": old},
    })
    fake.albums[src] = {"P1", "P2", "P3", "P4", "P5"}
    fake.albums[organized] = {"Q1"}
    monkeypatch.setattr(context, "client", lambda cfg: fake)
    return root, fake, src


def run(root, *argv):
    from gp2sm.organize import cli
    return cli.main(["--project", str(root), *argv])


def test_organize_end_to_end(project, capsys):
    root, fake, src = project
    assert run(root, "inventory") == 0
    st = State(str(root / "state.db"))
    assert st.one("SELECT COUNT(*) FROM images") == 5                      # the project's own folder isn't a source
    assert run(root, "plan") == 0
    plan = dict(st.q("SELECT image_key, target_name FROM plan"))
    assert plan == {"P1": "Phone 2023-05", "P2": "Screens 2023-06", "P3": "Photos Duplicates (review)",
                    "P4": "Unassigned Undated"}
    assert run(root, "apply") == 0 and "Dry run" in capsys.readouterr().out
    assert fake.albums[src] == {"P1", "P2", "P3", "P4", "P5"}
    assert run(root, "apply", "--yes") == 0
    assert fake.albums[src] == {"P5"}
    names = {fake.names.get(a, a): items for a, items in fake.albums.items()}
    assert names["Phone 2023-05"] == {"P1"} and names["Screens 2023-06"] == {"P2"}
    assert run(root, "verify") == 0
    assert run(root, "plan") == 0                                            # re-planning keeps done items
    assert dict(st.q("SELECT image_key, status FROM plan")) == {k: "done" for k in ("P1", "P2", "P3", "P4")}
    assert not os.path.exists(root / ".gp2sm.lock")


def test_collect_mode_plans_collects_and_parks_nothing():
    rows = planning.plan_organize([item("k", "2023-01-02", md5="same"), item("x", None, md5="same")],
                                  dict(SETTINGS, mode="collect"))
    assert {r["action"] for r in rows} == {"collect"}


def test_organize_collect_end_to_end(project):
    root, fake, src = project
    text = (root / "gp2sm.toml").read_text().replace('mode = "move"', 'mode = "collect"')
    (root / "gp2sm.toml").write_text(text)
    for step in ("inventory", "plan"):
        assert run(root, step) == 0
    assert run(root, "apply", "--yes") == 0
    names = {fake.names.get(a, a): items for a, items in fake.albums.items()}
    assert names["Phone 2023-05"] == {"P1", "P3"}                       # identical copies aren't parked
    assert fake.albums[src] == {"P1", "P2", "P3", "P4", "P5"}            # nothing left the uploader's album
    assert set(fake.item_album_ids("P1")) == {src, next(a for a, n in fake.names.items() if n == "Phone 2023-05")}
    assert run(root, "verify") == 0
    with pytest.raises(SystemExit, match="collected items too"):
        run(root, "delete-empty-sources", "--yes")
    assert run(root, "undo", "Phone 2023-05", "--yes") == 0                # removes only the collected copy
    assert fake.albums[src] == {"P1", "P2", "P3", "P4", "P5"}
    assert fake.item_album_ids("P1") == [src]


def test_delete_empty_targets_says_why_it_kept_an_album(project, capsys):
    root, fake, src = project
    for step in ("inventory", "plan"):
        run(root, step)
    run(root, "apply", "--yes")
    run(root, "undo", "Phone 2023-05", "--yes")               # empty again, but its items are planned once more
    capsys.readouterr()
    assert run(root, "delete-empty-targets", "--yes") == 0
    out = capsys.readouterr().out
    assert "Phone 2023-05: 1 items still planned for it" in out
    assert any(n == "Phone 2023-05" for n in fake.names.values())        # kept


def test_apply_refuses_while_another_run_holds_the_lock(project):
    root, fake, src = project
    for step in ("inventory", "plan"):
        run(root, step)
    (root / ".gp2sm.lock").write_text(f"{os.getppid()} organize apply")
    with pytest.raises(context.Locked):
        run(root, "apply", "--yes")
    assert fake.albums[src] == {"P1", "P2", "P3", "P4", "P5"}
