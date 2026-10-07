"""Resolve a command's project: --project DIR, or the nearest gp2sm.toml upward from the current folder.

Settings come from the project's gp2sm.toml; path options left unset (--index, --takeout-dir, --stage-dir, --db)
are filled from the project; credentials come from the per-user store.
"""

import atexit
import os

from gp2sm.project import credentials
from gp2sm.project.config import find_project, load


class NoProject(SystemExit):
    pass


def add_args(parser, paths=()):
    """--project, plus the given path options (defaults resolved later)."""
    parser.add_argument("--project", help="project folder with gp2sm.toml (default: nearest one upward from here)")
    flags = {"index": "--index", "takeout_dir": "--takeout-dir", "stage_dir": "--stage-dir", "db": "--db"}
    for name in paths:
        parser.add_argument(flags[name], dest=name, default=None)


def resolve(args):
    """Settings dict for the commands; also fills unset path attributes on args. Sets args.project_root."""
    root = os.path.abspath(args.project) if getattr(args, "project", None) else find_project()
    if not root:
        raise NoProject("no gp2sm project here: run `gp2sm init`, or pass --project DIR")
    pc = load(root)
    name = pc.get("destination", "credentials")
    creds = credentials.path(name) if credentials.exists(name) else None
    cfg = pc.tool_settings(credentials_file=creds)
    cfg["_credentials_name"] = name
    paths = {"index": pc.path(pc.get("takeout", "index")), "takeout_dir": pc.path(pc.get("takeout", "archives")),
             "stage_dir": pc.path("stage"), "db": pc.path(pc.get("takeout", "index"))}
    os.makedirs(os.path.dirname(cfg["log_file"]), exist_ok=True)
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
