import pytest

from gp2sm import __version__, cli


def test_help_lists_every_command(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for name in cli.COMMANDS:
        assert name in out


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_unknown_command_exits_2(capsys):
    assert cli.main(["nope"]) == 2


@pytest.mark.parametrize("name", list(cli.COMMANDS))
def test_every_command_module_has_main(name):
    import importlib
    target, _, attr = cli.COMMANDS[name][0].partition(":")
    assert callable(getattr(importlib.import_module(target), attr or "main"))


def test_services_lists_builtins():
    from gp2sm.cli import services
    lines = []
    assert services.main([], show=lines.append) == 0
    text = "\n".join(lines)
    assert "google-takeout" in text and "smugmug" in text


def test_top_level_verbs_pick_the_pipeline(tmp_path, monkeypatch):
    from gp2sm.cli import verbs
    from gp2sm.project import context
    from gp2sm.project.init import main as init_main
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    root = tmp_path / "proj"
    init_main([str(root), "--non-interactive", "--name", "T", "--timezone", "UTC"], show=lambda *a: None)
    calls = []
    monkeypatch.setattr(verbs, "runner", lambda pipeline: lambda argv: calls.append((pipeline, argv)) or 0)
    monkeypatch.setattr(context, "client", lambda cfg: None)
    with pytest.raises(SystemExit, match="nothing to do"):
        verbs.plan(["--project", str(root)], show=lambda *a: None)
    (root / "takeout" / "t.zip").write_bytes(b"")
    assert verbs.plan(["--project", str(root)], show=lambda *a: None) == 0
    assert [a[-1] for _, a in calls] == ["index", "inventory", "dedupe", "plan"]
    text = (root / "gp2sm.toml").read_text().replace('sources = []\n# where capture dates',
                                                     'sources = ["Uploads"]\n# where capture dates')
    (root / "gp2sm.toml").write_text(text)
    with pytest.raises(SystemExit, match="more than one pipeline"):
        verbs.apply(["--project", str(root)], show=lambda *a: None)
    calls.clear()
    assert verbs.apply(["organize", "--project", str(root), "--yes", "--limit", "5"], show=lambda *a: None) == 0
    assert calls == [("organize", ["--project", str(root), "apply", "--yes", "--limit", "5"])]
    with pytest.raises(SystemExit, match="isn't available for takeout"):
        verbs.undo(["takeout", "--project", str(root)], show=lambda *a: None)
