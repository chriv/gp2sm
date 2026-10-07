"""gp2sm services: list the photo services installed (built-in and plugins)."""

import argparse

from gp2sm.services import registry


def main(argv=None, show=print):
    argparse.ArgumentParser(prog="gp2sm services", description=__doc__).parse_args(argv)
    infos = registry.available()
    for kind in ("source", "destination"):
        show(f"{kind}s:")
        for info in sorted((i for i in infos.values() if i.kind == kind), key=lambda i: i.name):
            show(f"  {info.name:<16} {info.description}")
    return 0
