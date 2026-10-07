"""`gp2sm` command: dispatches to the tool modules (a richer project-based CLI is ROADMAP stage A2)."""

import importlib
import sys

from gp2sm import __version__

COMMANDS = {
    "auth": ("gp2sm.cli.auth", "sign in to a service and manage stored credentials"),
    "consolidate": ("gp2sm.organize.consolidate", "organize existing SmugMug albums (inventory/match/plan/apply/verify/...)"),
    "takeout-index": ("gp2sm.takeout.index", "index Google Takeout archives without extracting them"),
    "takeout-match": ("gp2sm.takeout.match", "pair metadata files and Live Photo clips; compare with consolidation state"),
    "content-match": ("gp2sm.organize.content_match", "perceptual matching of Takeout stills to existing SmugMug copies"),
    "takeout-upload": ("gp2sm.takeout.upload", "plan/stage/upload/verify Takeout items (Live Photo pairs, HEIC)"),
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
