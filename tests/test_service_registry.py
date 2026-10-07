import json

import pytest

from gp2sm.services import PhotoDestination, PhotoSource, registry
from gp2sm.services.registry import ServiceInfo
from tests.fakes.smugmug import FakeSmugMug


class FakeEP:
    def __init__(self, name, value, fn):
        self.name, self.value, self._fn = name, value, fn

    def load(self):
        return self._fn


def test_builtins_are_available():
    infos = registry.available()
    assert infos["smugmug"].kind == "destination" and infos["google-takeout"].kind == "source"


def test_builtin_destination_factory(tmp_path):
    cfg = tmp_path / "smugmug_config.json"
    cfg.write_text(json.dumps({"api_key": "k", "api_secret": "s", "oauth_token": "t", "oauth_token_secret": "ts"}))
    assert isinstance(registry.destination("smugmug", config=str(cfg)), PhotoDestination)


def test_builtin_source_factory(tmp_path):
    assert isinstance(registry.source("google-takeout", index_db=str(tmp_path / "x.db"), takeout_dir=str(tmp_path)),
                      PhotoSource)


def test_third_party_plugin_is_discovered(monkeypatch):
    info = ServiceInfo("fakesmug", "destination", lambda: FakeSmugMug())
    monkeypatch.setattr(registry, "_entry_points", lambda: [FakeEP("fakesmug", "pkg:service", lambda: info)])
    assert isinstance(registry.destination("fakesmug"), PhotoDestination)


def test_bad_plugins_are_ignored_or_rejected(monkeypatch):
    def broken():
        raise RuntimeError("boom")
    eps = [FakeEP("broken", "a:b", broken),
           FakeEP("invalid", "c:d", lambda: "not a ServiceInfo"),
           FakeEP("shadow", "e:f", lambda: ServiceInfo("smugmug", "destination", lambda: None)),
           FakeEP("liar", "g:h", lambda: ServiceInfo("liar", "destination", lambda: object()))]
    monkeypatch.setattr(registry, "_entry_points", lambda: eps)
    infos = registry.available()
    assert "broken" not in infos and "invalid" not in infos
    assert infos["smugmug"].factory is not eps[2].load()().factory          # built-in can't be replaced
    with pytest.raises(TypeError):
        registry.destination("liar")                                          # doesn't implement the protocol
    with pytest.raises(TypeError):
        registry.source("smugmug", config="x")                                # wrong kind
    with pytest.raises(KeyError):
        registry.destination("nope")
