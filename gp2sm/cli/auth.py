"""`gp2sm auth`: manage stored service credentials.

  gp2sm auth smugmug [--name NAME] [--api-key KEY]       interactive PIN sign-in (secret is prompted, hidden)
  gp2sm auth smugmug [--name NAME] --import FILE         reuse an existing smugmug_config.json
  gp2sm auth list
  gp2sm auth remove NAME --yes
"""

import argparse
import getpass
import json
import sys

from gp2sm.project import credentials
from gp2sm.smugmug import auth as smug_auth


def verify(creds):
    from gp2sm.smugmug.client import SmugMugClient
    client = SmugMugClient(creds["api_key"], creds["api_secret"], creds["oauth_token"], creds["oauth_token_secret"])
    return client.authuser().get("NickName")


def main(argv=None, verify_fn=verify, ask=input, ask_secret=getpass.getpass, show=print):
    p = argparse.ArgumentParser(prog="gp2sm auth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("smugmug")
    s.add_argument("--name", default="smugmug")
    s.add_argument("--api-key")
    s.add_argument("--import", dest="import_file", help="existing smugmug_config.json with api_key/secret + oauth tokens")
    s.add_argument("--no-verify", action="store_true", help="skip the test call to SmugMug")
    sub.add_parser("list")
    r = sub.add_parser("remove")
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "list":
        for n in credentials.names():
            show(n)
        return 0
    if args.cmd == "remove":
        if not args.yes:
            show(f"dry run: would remove stored credentials {args.name!r} ({credentials.path(args.name)}). Add --yes.")
            return 0
        credentials.remove(args.name)
        show(f"removed {args.name!r}")
        return 0

    if args.import_file:
        with open(args.import_file) as f:
            creds = smug_auth.check(json.load(f))
    else:
        api_key = args.api_key or ask("SmugMug API key: ").strip()
        api_secret = ask_secret("SmugMug API secret (hidden): ").strip()
        creds = smug_auth.pin_flow(api_key, api_secret, ask_pin=ask, show=show)
    if not args.no_verify:
        nickname = verify_fn(creds)
        show(f"verified: signed in as SmugMug user {nickname!r}")
    where = credentials.save(args.name, creds)
    show(f"saved credentials {args.name!r} to {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
