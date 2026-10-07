"""Per-user credential store, outside every project (so one sign-in serves many projects).

Location: $GP2SM_CONFIG_DIR, else the platform's user config folder:
  macOS:   ~/Library/Application Support/gp2sm
  Windows: %APPDATA%\\gp2sm
  other:   $XDG_CONFIG_HOME/gp2sm or ~/.config/gp2sm
Files are credentials/<name>.json, written atomically; on POSIX the folder is 0700 and files are 0600.
"""

import json
import os
import re
import sys
import tempfile

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def user_config_dir():
    if os.environ.get("GP2SM_CONFIG_DIR"):
        return os.environ["GP2SM_CONFIG_DIR"]
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/gp2sm")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "gp2sm")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "gp2sm")


def _dir():
    d = os.path.join(user_config_dir(), "credentials")
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(d, 0o700)
    return d


def path(name):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid credential name {name!r} (letters, digits, . _ -)")
    return os.path.join(_dir(), f"{name}.json")


def save(name, data):
    target = path(name)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def load(name):
    p = path(name)
    if not os.path.exists(p):
        raise FileNotFoundError(f"no stored credentials named {name!r}; run `gp2sm auth smugmug --name {name}`")
    with open(p) as f:
        return json.load(f)


def exists(name):
    return os.path.exists(path(name))


def names():
    return sorted(f[:-5] for f in os.listdir(_dir()) if f.endswith(".json") and not f.startswith("."))


def remove(name):
    os.remove(path(name))
