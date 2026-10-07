"""`gp2sm` command: dispatches to the tool modules (a richer project-based CLI is ROADMAP stage A2)."""

import importlib
import sys

from gp2sm import __version__

COMMANDS = {
    "init": ("gp2sm.project.init", "create a project folder with a commented gp2sm.toml"),
    "plan": ("gp2sm.cli.verbs:plan", "plan the project's work (dry; nothing on the destination changes)"),
    "apply": ("gp2sm.cli.verbs:apply", "carry out the plan (dry run unless --yes)"),
    "verify": ("gp2sm.cli.verbs:verify", "check the destination against what was done"),
    "report": ("gp2sm.cli.verbs:report", "what was planned and done, and why"),
    "undo": ("gp2sm.cli.verbs:undo", "undo an organize move for one album (dry run unless --yes)"),
    "status": ("gp2sm.cli.status", "show project settings, credentials, lock and state summary"),
    "services": ("gp2sm.cli.services", "list installed photo services (sources and destinations)"),
    "auth": ("gp2sm.cli.auth", "sign in to a service and manage stored credentials"),
    "takeout": ("gp2sm.takeout.cli", "import a Google Takeout (index, inventory, dedupe, review, plan, stage, upload, verify)"),
    "albums": ("gp2sm.albums.cli", "album names: date-sortable renames with review and undo"),
    "organize": ("gp2sm.organize.cli", "gather items already on the destination into dated, grouped albums (rules)"),
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
    target, _, attr = COMMANDS[argv[0]][0].partition(":")
    entry = getattr(importlib.import_module(target), attr or "main")
    return entry(argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
