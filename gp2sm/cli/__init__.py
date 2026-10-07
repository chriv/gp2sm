"""`gp2sm` command: dispatches to the tool modules (a richer project-based CLI is ROADMAP stage A2)."""

import importlib
import sys

from gp2sm import __version__

COMMANDS = {
    "init": ("gp2sm.project.init", "create a project folder with a commented gp2sm.toml"),
    "status": ("gp2sm.cli.status", "show project settings, credentials, lock and state summary"),
    "services": ("gp2sm.cli.services", "list installed photo services (sources and destinations)"),
    "auth": ("gp2sm.cli.auth", "sign in to a service and manage stored credentials"),
    "takeout": ("gp2sm.takeout.cli", "import a Google Takeout: inventory, dedupe (more steps coming in A3)"),
    "organize": ("gp2sm.organize.cli", "gather items already on the destination into dated, grouped albums (rules)"),
    "consolidate": ("gp2sm.organize.consolidate", "legacy: the first migration's consolidation (legacy-dated); new projects use organize"),
    "takeout-index": ("gp2sm.takeout.index", "index Google Takeout archives without extracting them"),
    "takeout-match": ("gp2sm.contrib.legacy_bridge.takeout_match", "legacy: link a Takeout index to the old transfer database"),
    "content-match": ("gp2sm.contrib.legacy_bridge.content_match", "legacy: perceptual matching of Takeout stills to old uploads"),
    "takeout-upload": ("gp2sm.contrib.legacy_bridge.takeout_upload", "legacy: the first migration's upload plan (stage/upload/verify shared)"),
    "place-clips": ("gp2sm.organize.place_clips", "place unsorted clips beside their stills by capture time"),
    "date-undated": ("gp2sm.organize.date_undated", "date items in undated albums from evidence and move them"),
}


def usage():
    lines = [f"gp2sm {__version__}", "", "usage: gp2sm <command> [options]   (gp2sm <command> --help for details)", ""]
    lines += [f"  {name:15} {help_}" for name, (_, help_) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    if argv[0] not in COMMANDS:
        print(f"gp2sm: unknown command {argv[0]!r}\n\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[argv[0]][0])
    return module.main(argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
