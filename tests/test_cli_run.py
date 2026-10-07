import argparse
import io
import os
import signal

import pytest

from gp2sm.cli import run
from gp2sm.state import State


@pytest.fixture
def st(tmp_path):
    return State(str(tmp_path / "s.db"))


def last_run(st):
    return st.q("SELECT command, status, summary FROM runs ORDER BY run_id DESC LIMIT 1")[0]


def capture():
    lines = []
    return lines, lambda *a, **k: lines.append(" ".join(map(str, a)))


def test_ok_run_is_recorded_and_summary_shown(st):
    lines, show = capture()
    assert run.run_command(st, "thing", argparse.Namespace(x=1), lambda: {"done": 3}, show=show) == 0
    assert tuple(last_run(st))[:2] == ("thing", "ok")
    assert '"done": 3' in lines[0]


def test_graceful_stop_is_recorded_as_stopped(st):
    lines, show = capture()

    def work():
        run.Stop.requested = True
        return {"done": 1}
    assert run.run_command(st, "thing", argparse.Namespace(), work, show=show) == 0
    assert last_run(st)["status"] == "stopped"
    assert any("rerun the same command" in s for s in lines)


def test_hard_interrupt_rolls_back_and_exits_130(st):
    lines, show = capture()

    def work():
        st.db.execute("INSERT INTO meta(key, value) VALUES('half', 'written')")
        raise KeyboardInterrupt
    assert run.run_command(st, "thing", argparse.Namespace(), work, show=show) == run.INTERRUPTED_EXIT
    assert last_run(st)["status"] == "interrupted"
    assert st.q("SELECT * FROM meta WHERE key='half'") == []
    assert any("Interrupted" in s for s in lines)


def test_failure_is_recorded_and_reraised(st):
    def work():
        raise ValueError("boom")
    with pytest.raises(ValueError):
        run.run_command(st, "thing", argparse.Namespace(), work, show=lambda *a, **k: None)
    assert last_run(st)["status"] == "failed" and "boom" in last_run(st)["summary"]


def test_stale_stop_flag_does_not_leak_into_next_run(st):
    run.Stop.requested = True
    run.run_command(st, "thing", argparse.Namespace(), lambda: None, show=lambda *a, **k: None)
    assert last_run(st)["status"] == "ok"


@pytest.mark.skipif(not hasattr(signal, "raise_signal") or os.name == "nt", reason="needs POSIX SIGINT delivery")
def test_first_ctrl_c_requests_stop_second_aborts():
    previous = signal.getsignal(signal.SIGINT)
    try:
        run.install_sigint()
        signal.raise_signal(signal.SIGINT)
        assert run.Stop.requested
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)
        run.Stop.requested = False


def test_add_yes_defaults_to_dry_run():
    p = argparse.ArgumentParser()
    run.add_yes(p, "do it")
    assert p.parse_args([]).yes is False and p.parse_args(["--yes"]).yes is True


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_progress_logs_periodically_when_not_a_terminal(caplog):
    clock = FakeClock()
    p = run.Progress(10, "moving", stream=io.StringIO(), every=30, clock=clock)
    with caplog.at_level("INFO", logger="gp2sm"):
        p.update()
        clock.t = 31
        p.update(failed=1)
        p.update()
        clock.t = 40
        p.update(7)
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[0].startswith("moving: 2/10 (20%), failed 1, ~")
    assert msgs[-1] == "moving: 10/10 (100%), failed 1"
    assert len(msgs) == 2


def test_progress_rewrites_one_line_on_a_terminal():
    class Tty(io.StringIO):
        def isatty(self):
            return True
    stream = Tty()
    p = run.Progress(4, "uploading", stream=stream, clock=FakeClock())
    p.update()
    p.update()
    p.close()
    assert stream.getvalue().count("\r") == 2 and stream.getvalue().endswith("\n")
    assert "uploading: 2/4 (50%)" in stream.getvalue()


def test_duration_format():
    assert run._duration(45) == "45s"
    assert run._duration(600) == "10m"
    assert run._duration(7200) == "2.0h"
