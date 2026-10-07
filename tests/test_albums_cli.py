"""`gp2sm albums` against the fake destination: plan, review, approve, apply, verify, undo."""

import pytest

from gp2sm.albums import cli
from gp2sm.project import context
from gp2sm.project.init import main as init_main
from gp2sm.state import State
from tests.fakes.smugmug import FakeSmugMug


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    root = tmp_path / "proj"
    init_main([str(root), "--non-interactive", "--name", "T", "--timezone", "UTC"], show=lambda *a: None)
    text = (root / "gp2sm.toml").read_text()
    text = text.replace('# folders or albums (by name) whose album names are checked; ["/"] = the whole account\nscope = []',
                        'scope = ["Family"]').replace('exclude = []', 'exclude = ["*Auto Upload*"]')
    (root / "gp2sm.toml").write_text(text)
    fake = FakeSmugMug()
    fam = fake.ensure_folder_path(fake.root_folder(), "Family")
    ids = {n: fake.ensure_album(fam, n)[0] for n in
           ["Beach Trip 06-2019", "Christmas 2015", "Lake weekend", "Phone Auto Upload", "2019-06 Beach Trip",
            "Party 03-04-2021", "Stuck 05-2018", "Spread out", "Picnic 07-2020", "2020-07 Picnic"]}
    for i, d in enumerate(["2021-07-02T10:00:00", "2021-07-04T09:00:00", "2021-07-05T18:00:00",
                           "2021-07-03T08:00:00", "2021-07-03T09:00:00"]):
        fake.items[f"L{i}"] = {"name": f"l{i}.jpg", "capture_time": d}
        fake.albums[ids["Lake weekend"]].add(f"L{i}")
    for i, d in enumerate(["2015-01-01T00:00:00", "2019-06-01T00:00:00"]):
        fake.items[f"S{i}"] = {"name": f"s{i}.jpg", "capture_time": d}
        fake.albums[ids["Spread out"]].add(f"S{i}")
    fake.frozen_names.add(ids["Stuck 05-2018"])               # a rename that "succeeds" but doesn't change
    monkeypatch.setattr(context, "client", lambda cfg: fake)
    return root, fake, ids


def run(root, *argv):
    return cli.main(["--project", str(root), *argv])


def changes(root):
    st = State(str(root / "state.db"))
    return {r["old_value"]: (r["new_value"], r["status"]) for r in st.q("SELECT * FROM album_changes")}


def test_names_end_to_end(project, capsys):
    root, fake, ids = project
    assert run(root, "inventory") == 0
    assert run(root, "plan") == 0
    c = changes(root)
    assert c["Beach Trip 06-2019"] == ("2019-06 Beach Trip", "review")     # collides with an existing album
    assert c["Christmas 2015"] == ("2015 Christmas", "review")             # year only: medium
    assert c["Lake weekend"] == ("2021-07 Lake weekend", "review")         # dated from its photos
    assert c["Party 03-04-2021"][1] == "review"                            # day/month could be swapped
    assert c["Stuck 05-2018"] == ("2018-05 Stuck", "planned")
    assert c["Picnic 07-2020"][1] == "review"                              # "2020-07 Picnic" exists too
    assert "Phone Auto Upload" not in c and "Spread out" not in c and "2019-06 Beach Trip" not in c
    assert run(root, "report") == 0 and "For review" in capsys.readouterr().out

    assert run(root, "approve", "--name", "Lake*", "--name", "Christmas*") == 0
    assert run(root, "apply") == 0 and "Dry run" in capsys.readouterr().out
    assert fake.names[ids["Lake weekend"]] == "Lake weekend"
    fake.rename_album(ids["Christmas 2015"], "Xmas (renamed by hand)")    # someone renames it meanwhile
    assert run(root, "apply", "--yes") == 0
    c = changes(root)
    assert c["Lake weekend"] == ("2021-07 Lake weekend", "done")
    assert c["Stuck 05-2018"][1] == "failed"                               # the read-back caught the no-op
    assert c["Christmas 2015"][1] == "skipped"
    assert fake.names[ids["Lake weekend"]] == "2021-07 Lake weekend"
    assert run(root, "verify") == 0

    assert run(root, "undo", "--yes") == 0
    assert fake.names[ids["Lake weekend"]] == "Lake weekend"
    assert changes(root)["Lake weekend"][1] == "undone"
    assert run(root, "plan") == 0 and changes(root)["Lake weekend"] == ("2021-07 Lake weekend", "review")


def test_sample_limits_inventory(project):
    root, fake, ids = project
    assert run(root, "inventory", "--sample", "3") == 0
    assert State(str(root / "state.db")).one("SELECT COUNT(*) FROM albums_seen") == 3


@pytest.fixture
def policy_project(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    root = tmp_path / "pol"
    init_main([str(root), "--non-interactive", "--name", "P", "--timezone", "UTC"], show=lambda *a: None)
    with open(root / "gp2sm.toml", "a") as f:
        f.write('\n[[policy]]\nscope = ["*"]\nsort = "date_taken"\n'
                '\n[[policy]]\nscope = ["Family/*"]\nexclude = ["*Auto Upload*"]\ndownloads = true\n'
                'download_size = "x3large"\n'
                '\n[[policy]]\nscope = ["Hidden/*"]\nprivacy = "public"\n')
    fake = FakeSmugMug()
    fam = fake.ensure_folder_path(fake.root_folder(), "Family")
    hid = fake.ensure_folder_path(fake.root_folder(), "Hidden")
    ids = {"beach": fake.ensure_album(fam, "Beach")[0], "auto": fake.ensure_album(fam, "Phone Auto Upload")[0],
           "secret": fake.ensure_album(hid, "Secret")[0]}
    for key in ("beach", "auto", "secret"):
        fake.items[f"I{key}"] = {"name": f"{key}.jpg"}
        fake.albums[ids[key]].add(f"I{key}")
    ids["empty"] = fake.ensure_album(fam, "Empty one")[0]
    fake.private_folders.add("Hidden")
    monkeypatch.setattr(context, "client", lambda cfg: fake)
    return root, fake, ids


def test_settings_audit_fix_verify_undo(policy_project, capsys):
    root, fake, ids = policy_project
    assert run(root, "audit") == 0
    st = State(str(root / "state.db"))
    planned = {(r["album_id"], r["field"]): (r["old_value"], r["new_value"]) for r in
               st.q("SELECT * FROM album_changes WHERE kind='setting' AND status='planned'")}
    assert planned[(ids["beach"], "downloads")] == ("false", "true")
    assert planned[(ids["beach"], "download_size")] == ('"original"', '"x3large"')
    assert (ids["auto"], "downloads") not in planned                      # excluded from the Family policy
    assert (ids["secret"], "privacy") in planned                          # its own setting can still be fixed ...
    notes = [r["note"] for r in st.q("SELECT note FROM album_changes WHERE kind='finding'")]
    assert any("containing folder makes it private" in n for n in notes)   # ... but the folder keeps it private
    assert "empty" in notes
    assert run(root, "report") == 0 and "Setting fixes planned" in capsys.readouterr().out

    assert run(root, "fix") == 0 and "Dry run" in capsys.readouterr().out
    assert fake.album_settings(ids["beach"])["downloads"] is False
    fake.set_album_settings(ids["secret"], {"privacy": "unlisted"})      # someone changes it after the audit
    assert run(root, "fix", "--yes") == 0
    beach = fake.album_settings(ids["beach"])
    assert (beach["downloads"], beach["download_size"]) == (True, "x3large")   # downloads first, then the size
    assert st.one("SELECT status FROM album_changes WHERE album_id=? AND field='privacy'", ids["secret"]) == "skipped"
    assert run(root, "verify") == 0

    assert run(root, "undo", "--yes") == 0
    beach = fake.album_settings(ids["beach"])
    assert (beach["downloads"], beach["download_size"]) == (False, "original")
    assert st.one("SELECT COUNT(*) FROM album_changes WHERE kind='setting' AND status='done'") == 0   # all undone
    assert fake.album_settings(ids["secret"])["privacy"] == "unlisted"   # left as the person set it


def test_unknown_current_values_go_to_review(policy_project):
    root, fake, ids = policy_project
    fake.settings.setdefault(ids["beach"], {})["sort"] = "Shuffle (app-made)"   # a value gp2sm doesn't know
    assert run(root, "audit") == 0
    st = State(str(root / "state.db"))
    row = st.q("SELECT status, note FROM album_changes WHERE album_id=? AND field='sort'", ids["beach"])[0]
    assert row["status"] == "review" and "couldn't be undone" in row["note"]
    assert run(root, "approve", "--name", "Beach") == 0
    assert st.one("SELECT status FROM album_changes WHERE album_id=? AND field='sort'", ids["beach"]) == "planned"
