import pytest

from gp2sm.project import credentials, init
from gp2sm.project.config import load


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "cfg"))


def test_non_interactive_init_writes_valid_commented_config(tmp_path):
    out = []
    proj = tmp_path / "proj"
    assert init.main([str(proj), "--non-interactive", "--prefix", "Family", "--timezone", "America/Chicago",
                      "--source", "Old-Import"], show=out.append) == 0
    cfg = load(str(proj))
    assert cfg.get("albums", "photo") == "Family {yyyy}-{mm}" and cfg.get("project", "timezone") == "America/Chicago"
    assert cfg.get("organize", "sources") == ["Old-Import"]
    text = (proj / "gp2sm.toml").read_text()
    assert "# split an album into '- Part N'" in text                 # help text is in the file
    assert (proj / "logs").is_dir() and (proj / "takeout").is_dir()
    assert any("gp2sm auth smugmug" in line for line in out)          # no credentials yet -> told to sign in


def test_interactive_answers_and_defaults(tmp_path):
    answers = iter(["My Project", "", "Imports", "Trip", ""])          # blank = accept default
    proj = tmp_path / "p2"
    init.main([str(proj)], ask=lambda _: next(answers), show=lambda _: None)
    cfg = load(str(proj))
    assert cfg.get("project", "name") == "My Project" and cfg.get("destination", "folder") == "Imports"
    assert cfg.get("albums", "undated_photo") == "Trip Undated"


def test_refuses_to_overwrite_without_force(tmp_path):
    proj = tmp_path / "p3"
    init.main([str(proj), "--non-interactive"], show=lambda _: None)
    assert init.main([str(proj), "--non-interactive"], show=lambda _: None) == 1
    assert init.main([str(proj), "--non-interactive", "--force"], show=lambda _: None) == 0


def test_no_auth_hint_when_credentials_exist(tmp_path):
    credentials.save("smugmug", {"api_key": "k", "api_secret": "s", "oauth_token": "t", "oauth_token_secret": "ts"})
    out = []
    init.main([str(tmp_path / "p4"), "--non-interactive"], show=out.append)
    assert not any("gp2sm auth" in line for line in out)


def test_strings_with_quotes_round_trip(tmp_path):
    proj = tmp_path / "p5"
    init.main([str(proj), "--non-interactive", "--folder", 'Dad\'s "Best" Photos'], show=lambda _: None)
    assert load(str(proj)).get("destination", "folder") == 'Dad\'s "Best" Photos'
