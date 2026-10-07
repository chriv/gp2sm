"""Resolve a command's settings and paths: from a project (gp2sm.toml) or a legacy JSON config.

Order: --config FILE (legacy JSON) > --project DIR or the nearest gp2sm.toml upward from the current folder >
an existing data/consolidate.json (legacy layout) > error suggesting `gp2sm init`.
Path options left unset (--index, --takeout-dir, --stage-dir, --state, --db) are filled from the same source.
"""

import atexit
import os

from gp2sm.project import credentials
from gp2sm.project.config import find_project, load, takeout_policies

LEGACY_CONFIG = "data/consolidate.json"
LEGACY_PATHS = {"index": "data/takeout_index.db", "takeout_dir": "data/takeout", "stage_dir": "data/stage",
                "state": "data/consolidation.db", "db": "data/takeout_index.db"}


class NoProject(SystemExit):
    pass


def add_args(parser, paths=()):
    """--project/--config, plus the given path options (defaults resolved later)."""
    parser.add_argument("--project", help="project folder with gp2sm.toml (default: nearest one upward from here)")
    parser.add_argument("--config", default=None, help="legacy JSON settings file (instead of a project)")
    flags = {"index": "--index", "takeout_dir": "--takeout-dir", "stage_dir": "--stage-dir", "state": "--state",
             "db": "--db"}
    for name in paths:
        parser.add_argument(flags[name], dest=name, default=None)


def resolve(args):
    """Settings dict for the tools; also fills unset path attributes on args. Sets args.project_root."""
    from gp2sm.organize.consolidate import load_config  # legacy JSON loader
    root = None
    if not getattr(args, "config", None):
        root = os.path.abspath(args.project) if getattr(args, "project", None) else find_project()
    if root:
        pc = load(root)
        name = pc.get("destination", "credentials")
        creds = credentials.path(name) if credentials.exists(name) else None
        cfg = pc.tool_settings(credentials_file=creds)
        cfg["_credentials_name"] = name
        paths = {"index": pc.path(pc.get("takeout", "index")), "takeout_dir": pc.path(pc.get("takeout", "archives")),
                 "stage_dir": pc.path("stage"), "state": pc.state_db, "db": pc.path(pc.get("takeout", "index"))}
        os.makedirs(os.path.dirname(cfg["log_file"]), exist_ok=True)
    else:
        config = getattr(args, "config", None) or (LEGACY_CONFIG if os.path.exists(LEGACY_CONFIG) else None)
        if not config:
            raise NoProject("no gp2sm project here: run `gp2sm init` (or pass --project DIR / --config FILE)")
        cfg = load_config(config)
        cfg.setdefault("takeout", takeout_policies())
        paths = dict(LEGACY_PATHS, state=cfg["state_db"])
    for name, value in paths.items():
        if hasattr(args, name) and getattr(args, name) is None:
            setattr(args, name, value)
    args.project_root = root
    return cfg


def client(cfg):
    """The destination client for these settings (needs stored credentials)."""
    from gp2sm.smugmug.client import SmugMugClient
    if not cfg.get("smugmug_config"):
        name = cfg.get("_credentials_name", "smugmug")
        raise NoProject(f"no stored credentials {name!r}: run `gp2sm auth smugmug --name {name}`")
    return SmugMugClient.from_config_file(cfg["smugmug_config"])


# ------------------------------------------------------------------ project lock

class Locked(SystemExit):
    pass


def _alive(pid):
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: exists
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock(state_db, label):
    """Exclusive lock next to the state DB for commands that change the destination. Stale locks are taken over."""
    path = os.path.join(os.path.dirname(os.path.abspath(state_db)), ".gp2sm.lock")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                pid, held_by = open(path).read().split(" ", 1)
                pid = int(pid)
            except (OSError, ValueError):
                pid, held_by = -1, "?"
            if pid > 0 and _alive(pid):
                raise Locked(f"another gp2sm command is running on this project (pid {pid}: {held_by.strip()}); "
                             f"wait for it, or remove {path} if you're sure it isn't") from None
            os.remove(path)  # stale: owner is gone
            continue
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {label}")
        atexit.register(release_lock, path)
        return path
    raise Locked(f"could not acquire {path}")


def release_lock(path):
    """Remove the lock if this process still holds it (idempotent)."""
    if not path:
        return
    try:
        pid = int(open(path).read().split(" ", 1)[0])
    except (OSError, ValueError):
        return
    if pid == os.getpid():
        os.remove(path)
