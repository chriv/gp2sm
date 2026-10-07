"""Behavior shared by every command: dry run by default, graceful Ctrl-C, run bookkeeping and progress display.

- Commands that change the destination only act with --yes; without it they show what would happen.
- The first Ctrl-C asks the command to stop after the work in flight (Stop.requested); a second one aborts.
  Either way the state database is left consistent: writes are recorded before they're sent, and anything
  whose outcome is unknown is checked against the server on the next run.
"""

import json
import logging
import os
import signal
import sys
import time

from gp2sm.project.context import release_lock

log = logging.getLogger("gp2sm")

INTERRUPTED_EXIT = 130


class Stop:
    requested = False


def setup_logging(log_file, debug=False):
    """Everything to the project's log file; INFO and up (DEBUG with --debug) to the terminal. Calling it
    again replaces the handlers instead of stacking them."""
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for old in [h for h in root.handlers if getattr(h, "_gp2sm", False)]:
        root.removeHandler(old)
        old.close()
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    for h in (fh, ch):
        h._gp2sm = True
        root.addHandler(h)
    for noisy in ("urllib3", "requests_oauthlib", "oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def add_yes(parser, action):
    """--yes for a command that changes the destination; without it the command is a dry run."""
    parser.add_argument("--yes", action="store_true",
                        help=f"actually {action} (default: dry run showing what would happen)")


def dry_run_footer(show=print):
    show("\nDry run: nothing was changed. Rerun with --yes to do this.")


def install_sigint():
    """First Ctrl-C: finish the work in flight, then stop. Second: abort now."""
    Stop.requested = False

    def handler(sig, frame):
        if Stop.requested:
            raise KeyboardInterrupt
        Stop.requested = True
        log.warning("stop requested; finishing the work in flight (Ctrl-C again to abort now)")
    signal.signal(signal.SIGINT, handler)


def run_command(st, name, args, fn, show=print, lock=None):
    """Run fn() as a recorded run: ok | stopped | interrupted | failed. Prints the summary; returns the exit code.
    Releases `lock` (from context.acquire_lock) when done."""
    try:
        return _run(st, name, args, fn, show)
    finally:
        release_lock(lock)


def _run(st, name, args, fn, show):
    Stop.requested = False
    st.start_run(name, vars(args))
    try:
        summary = fn()
    except KeyboardInterrupt:
        st.db.rollback()
        st.event("run_interrupted", level="warning")
        st.finish_run("interrupted")
        show("\nInterrupted. Progress so far is saved; rerun the same command to continue "
             "(anything left mid-flight is checked against the server first).", file=sys.stderr)
        return INTERRUPTED_EXIT
    except BaseException as e:
        st.db.rollback()
        st.event("run_failed", level="error", error=repr(e))
        st.finish_run("failed", {"error": repr(e)})
        raise
    st.finish_run("stopped" if Stop.requested else "ok", summary)
    if summary is not None:
        show(json.dumps(summary, indent=1, default=str))
    if Stop.requested:
        show("Stopped early at your request; rerun the same command to continue.", file=sys.stderr)
    return 0


class Progress:
    """A single updating line on a terminal; otherwise a log line at most every `every` seconds."""

    def __init__(self, total, label, stream=None, every=30.0, clock=time.monotonic):
        self.total, self.label, self.done = total, label, 0
        self.stream = stream or sys.stderr
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.every, self.clock = every, clock
        self.start = self.last = clock()
        self.counts = {}

    def update(self, n=1, **counts):
        self.done += n
        for k, v in counts.items():
            self.counts[k] = self.counts.get(k, 0) + v
        t = self.clock()
        if self.tty:
            self.stream.write("\r" + self.line() + "\033[K")
            self.stream.flush()
        elif t - self.last >= self.every or self.done == self.total:
            self.last = t
            log.info("%s", self.line())

    def line(self):
        pct = f" ({100 * self.done // self.total}%)" if self.total else ""
        extra = "".join(f", {k} {v}" for k, v in self.counts.items() if v)
        rate = self.done / max(self.clock() - self.start, 1e-9)
        eta = ""
        if self.total and 0 < self.done < self.total and rate > 0:
            eta = f", ~{_duration((self.total - self.done) / rate)} left"
        return f"{self.label}: {self.done}/{self.total}{pct}{extra}{eta}"

    def close(self):
        if self.tty:
            self.stream.write("\n")
            self.stream.flush()


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"
