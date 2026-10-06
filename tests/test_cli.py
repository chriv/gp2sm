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
    assert callable(importlib.import_module(cli.COMMANDS[name][0]).main)
