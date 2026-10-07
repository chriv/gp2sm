"""Top-level verbs: `gp2sm plan | apply | verify | report | undo`, run against the project's pipeline.

A project's pipelines are found from its config: `takeout` when its takeout folder holds archives,
`organize` when [organize] sources is set. If both apply, name one: `gp2sm plan organize`.
Each verb runs the pipeline's steps in order and stops at the first one that fails. Extra options
(--yes, --limit, --target, ...) go to the last step.

  plan     takeout: index -> inventory -> dedupe -> plan        organize: inventory -> plan
  apply    takeout: stage -> upload                              organize: apply
  verify   verify                                                verify
  report   report                                                report
  undo     (not available for takeout imports)                   undo ALBUM
"""

import argparse
import os

from gp2sm.project import context
from gp2sm.takeout.archive import list_archives

STEPS = {
    "takeout": {"plan": ["index", "inventory", "dedupe", "plan"], "apply": ["stage", "upload"],
                "verify": ["verify"], "report": ["report"]},
    "organize": {"plan": ["inventory", "plan"], "apply": ["apply"], "verify": ["verify"], "report": ["report"],
                 "undo": ["undo"]},
}


def pipelines(cfg, args):
    found = []
    if args.takeout_dir and os.path.isdir(args.takeout_dir) and list_archives(args.takeout_dir):
        found.append("takeout")
    if cfg.get("organize", {}).get("sources"):
        found.append("organize")
    return found


def runner(pipeline):
    if pipeline == "takeout":
        from gp2sm.takeout import cli
        return cli.main
    from gp2sm.organize import cli
    return cli.main


def make_main(verb):
    def main(argv=None, show=print):
        p = argparse.ArgumentParser(prog=f"gp2sm {verb}", description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument("pipeline", nargs="?", choices=list(STEPS), help="which pipeline (if the project has both)")
        context.add_args(p, paths=("takeout_dir",))
        args, extra = p.parse_known_args(argv)
        cfg = context.resolve(args)
        available = pipelines(cfg, args)
        if args.pipeline:
            chosen = args.pipeline
        elif len(available) == 1:
            chosen = available[0]
        elif available:
            raise SystemExit(f"this project has more than one pipeline {available}; say which: gp2sm {verb} "
                             f"{available[0]}")
        else:
            raise SystemExit("nothing to do: put Takeout archives in the project's takeout folder, or set "
                             "[organize] sources in gp2sm.toml")
        steps = STEPS[chosen].get(verb)
        if not steps:
            raise SystemExit(f"`{verb}` isn't available for {chosen}")
        common = ["--project", args.project] if args.project else []
        run = runner(chosen)
        for i, step in enumerate(steps):
            show(f"== {chosen} {step}")
            last = i == len(steps) - 1
            code = run(common + [step] + (extra if last else []))
            if code:
                return code
        return 0
    return main


plan = make_main("plan")
apply = make_main("apply")
verify = make_main("verify")
report = make_main("report")
undo = make_main("undo")
