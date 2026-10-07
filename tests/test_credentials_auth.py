import json
import os
import stat

import pytest

from gp2sm.cli import auth as auth_cli
from gp2sm.project import credentials
from gp2sm.smugmug import auth as smug_auth

CREDS = {"api_key": "k", "api_secret": "s", "oauth_token": "t", "oauth_token_secret": "ts"}


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "cfg"))


def test_store_roundtrip_and_permissions():
    where = credentials.save("smugmug", CREDS)
    assert credentials.load("smugmug") == CREDS and credentials.names() == ["smugmug"]
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(where).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(where)).st_mode) == 0o700


def test_bad_names_rejected():
    with pytest.raises(ValueError):
        credentials.path("../escape")


def test_missing_credentials_message():
    with pytest.raises(FileNotFoundError, match="gp2sm auth smugmug"):
        credentials.load("nope")


class FakeOAuth:
    def __init__(self, key, client_secret, callback_uri):
        assert callback_uri == "oob"

    def fetch_request_token(self, url):
        return {}

    def authorization_url(self, url):
        return url + "&oauth_token=REQ"

    def fetch_access_token(self, url, verifier):
        assert verifier == "123456"
        return {"oauth_token": "AT", "oauth_token_secret": "ATS"}


def test_pin_flow():
    shown = []
    creds = smug_auth.pin_flow("k", "s", ask_pin=lambda _: " 123456 ", show=shown.append, session_factory=FakeOAuth)
    assert creds == {"api_key": "k", "api_secret": "s", "oauth_token": "AT", "oauth_token_secret": "ATS"}
    assert "oauth_token=REQ" in shown[0]


def test_cli_import_verifies_then_saves(tmp_path):
    f = tmp_path / "smugmug_config.json"
    f.write_text(json.dumps({**CREDS, "album_name": "ignored extra"}))
    out = []
    assert auth_cli.main(["smugmug", "--import", str(f)], verify_fn=lambda c: "someone", show=out.append) == 0
    assert credentials.load("smugmug") == CREDS
    assert any("someone" in line for line in out)


def test_cli_import_rejects_placeholders(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({**CREDS, "oauth_token": "YOUR_SMUGMUG_OAUTH_TOKEN"}))
    with pytest.raises(ValueError, match="oauth_token"):
        auth_cli.main(["smugmug", "--import", str(f)], verify_fn=lambda c: "x", show=lambda _: None)


def test_cli_remove_is_dry_run_without_yes():
    credentials.save("old", CREDS)
    auth_cli.main(["remove", "old"], show=lambda _: None)
    assert credentials.exists("old")
    auth_cli.main(["remove", "old", "--yes"], show=lambda _: None)
    assert not credentials.exists("old")
