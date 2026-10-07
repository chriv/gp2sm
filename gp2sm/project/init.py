"""`gp2sm init`: create a project folder with a commented gp2sm.toml."""

import argparse
import json
import os
import sys

from gp2sm.project import credentials
from gp2sm.project.config import CONFIG_NAME, SCHEMA, load

SECTION_HELP = {
    "project": "General",
    "destination": "Where items go",
    "albums": "Album names and limits",
    "consolidate": "Reorganizing existing albums (`gp2sm consolidate`)",
    "takeout": "Google Takeout import",
    "run": "Performance",
}


def local_timezone():
    """Best-effort IANA name of the local time zone, else UTC."""
    if os.environ.get("TZ") and "/" in os.environ["TZ"]:
        return os.environ["TZ"]
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def render(values):
    """A commented gp2sm.toml for the given section -> key -> value mapping (defaults for anything missing)."""
    lines = ["# gp2sm project configuration. Paths are relative to this folder.",
             "# Reference: every key, its meaning and default, is listed below.", ""]
    for section, keys in SCHEMA.items():
        lines.append(f"# --- {SECTION_HELP.get(section, section)}")
        lines.append(f"[{section}]")
        for key, (_, default, help_) in keys.items():
            lines.append(f"# {help_}")
            lines.append(f"{key} = {_toml(values.get(section, {}).get(key, default))}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None, ask=input, show=print):
    p = argparse.ArgumentParser(prog="gp2sm init", description="Create a gp2sm project folder with a commented gp2sm.toml.")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument("--name")
    p.add_argument("--timezone")
    p.add_argument("--folder", help="destination folder for this project's albums")
    p.add_argument("--prefix", help="album name prefix, e.g. 'Family' -> 'Family {yyyy}-{mm}'")
    p.add_argument("--credentials", default=None, help="stored credential name (default: smugmug)")
    p.add_argument("--source", action="append", default=[], help="album path substring to consolidate from (repeatable)")
    p.add_argument("--non-interactive", action="store_true", help="don't ask; use flags and defaults")
    p.add_argument("--force", action="store_true", help="overwrite an existing gp2sm.toml")
    args = p.parse_args(argv)

    root = os.path.abspath(args.directory)
    target = os.path.join(root, CONFIG_NAME)
    if os.path.exists(target) and not args.force:
        show(f"{target} already exists; not overwriting (use --force).")
        return 1

    def pick(flag, prompt, default):
        if flag is not None:
            return flag
        if args.non_interactive:
            return default
        answer = ask(f"{prompt} [{default}]: ").strip()
        return answer or default

    name = pick(args.name, "Project name", os.path.basename(root))
    tz = pick(args.timezone, "Time zone for capture dates", local_timezone())
    folder = pick(args.folder, "Destination folder for this project's albums", name)
    prefix = pick(args.prefix, "Album name prefix", "Photos")
    creds = pick(args.credentials, "Stored credentials to use", "smugmug")
    values = {
        "project": {"name": name, "timezone": tz},
        "destination": {"folder": folder, "credentials": creds},
        "albums": {"photo": f"{prefix} {{yyyy}}-{{mm}}", "video": f"{prefix} {{yyyy}}-{{mm}}",
                   "undated_photo": f"{prefix} Undated", "undated_video": f"{prefix} Videos Undated",
                   "duplicates": f"{prefix} Duplicates (review)"},
        "consolidate": {"sources": args.source},
    }
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(root, "takeout"), exist_ok=True)
    with open(target, "w") as f:
        f.write(render(values))
    load(root)  # the file we wrote must validate
    show(f"created {target}")
    show("\nnext steps:")
    if not credentials.exists(creds):
        show(f"  gp2sm auth smugmug --name {creds}      # sign in once (stored outside the project)")
    show(f"  edit {target}      # review album names, sources, and limits")
    show(f"  gp2sm status --project {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
