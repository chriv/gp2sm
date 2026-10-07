"""The configuration reference (docs/config.md), generated from the schema so it can't drift from the code.

Regenerate with: python -m gp2sm.project.reference > docs/config.md   (a test checks the file is current)
"""

import json

from gp2sm.project.config import SCHEMA
from gp2sm.project.init import SECTION_HELP
from gp2sm.services.base import ALBUM_SETTING_VALUES


def _value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return json.dumps(v) if isinstance(v, str) else str(v)


def _type(kind):
    if isinstance(kind, tuple):
        return "one of " + ", ".join(_value(v) for v in kind)
    return {str: "text", int: "whole number (1 or more)", list: "list of text", "count": "whole number (0 or more)",
            "template": "album name template", "name_template": "album name template", "timezone": "time zone name",
            "rules": "list of rules"}.get(kind, str(kind))


def markdown():
    lines = ["# Configuration reference (`gp2sm.toml`)", "",
             "Every project has a `gp2sm.toml`; `gp2sm init` writes one with every key and its help text.",
             "Paths are relative to the project folder. Unknown keys and invalid values are reported all at once.",
             "This page is generated from the code (`python -m gp2sm.project.reference`).", ""]
    for section, keys in SCHEMA.items():
        lines += [f"## `[{section}]`: {SECTION_HELP.get(section, section)}", "",
                  "| key | type | default | meaning |", "|---|---|---|---|"]
        for key, (kind, default, help_) in keys.items():
            lines.append(f"| `{key}` | {_type(kind)} | `{_value(default)}` | {help_} |")
        lines.append("")
    lines += ["## `[[policy]]`: album settings policy (`gp2sm albums audit`, `fix`)", "",
              "Ordered entries; for each setting the last matching entry wins. Each entry needs a `scope`",
              "(folder/album globs matched against \"folder/name\" and the album name; `[\"*\"]` = every album) and may",
              "have an `exclude` list. Settings:", "",
              "| setting | values |", "|---|---|"]
    lines += [f"| `{k}` | {', '.join(_value(v) for v in vs)} |" for k, vs in ALBUM_SETTING_VALUES.items()]
    lines += ["", "`download_size` is only checked together with `downloads = true` (SmugMug resets it when downloads",
              "are turned off). A setting a containing folder overrides (a Private folder makes everything in it",
              "private) is reported, not changed.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    print(markdown(), end="")
