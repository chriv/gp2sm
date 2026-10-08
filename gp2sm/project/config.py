"""gp2sm.toml: schema, validation, and conversion to the settings the tools use.

Every problem is reported at once (unknown keys, wrong types, bad templates or time zones), so a user can fix
their config in one pass. Paths in the file are relative to the project folder.
"""

import os
import string
import tomllib
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gp2sm.services.base import ALBUM_SETTING_VALUES

CONFIG_NAME = "gp2sm.toml"

# section -> key -> (type, default, help). Types: str, int (positive), "count" (0 or more), list (of str),
# "template", "timezone", "rules" (an array of tables), or a tuple of allowed strings (a choice).
DATE_SOURCES = ("camera", "filename", "album", "upload")
RULE_KEYS = ("name", "album", "make", "model", "filename")
SCHEMA = {
    "project": {
        "name": (str, "", "a name for this project (shown in reports)"),
        "timezone": ("timezone", "UTC", "time zone for capture dates and month albums, e.g. America/New_York"),
    },
    "destination": {
        "service": (str, "smugmug", "destination service (see `gp2sm services`)"),
        "credentials": (str, "smugmug", "name of the stored credentials (see `gp2sm auth`)"),
        "folder": (str, "Consolidated", "folder that holds this project's albums"),
    },
    "albums": {
        "photo": ("template", "Photos {yyyy}-{mm}",
                  "dated album name; {yyyy} and {mm} come from the capture date, {group} from [[organize.group]]; "
                  "a / puts it in subfolders, e.g. {yyyy}/{yyyy}-{mm}"),
        "video": ("template", "Photos {yyyy}-{mm}", "dated album for videos (same as photo keeps them together)"),
        "undated_photo": (str, "Photos Undated", "album for photos with no confident date"),
        "undated_video": (str, "Videos Undated", "album for videos with no confident date"),
        "duplicates": (str, "Duplicates (review)", "album for byte-identical extra copies (moved, not deleted)"),
        "soft_cap": (int, 4000, "split an album into '- Part N' above this many items"),
        "hard_cap": (int, 5000, "never put more than this many items in one album"),
    },
    "takeout": {
        "archives": (str, "takeout", "folder with the Google Takeout archives (.zip or .tgz)"),
        "index": (str, "takeout_index.db", "Takeout index database"),
        "heic": (("convert", "keep"), "convert",
                 "HEIC photos: convert to JPEG here (EXIF kept) or upload as is (the destination may convert)"),
        "live_clips": (("pair", "separate", "skip"), "pair",
                       "Live Photo motion clips: pair (same name, next to the still), separate (video albums), skip"),
        "unpaired_clips": (("dated", "undated", "skip"), "dated",
                           "clips with no matching still: dated video album by their own time, the undated album, or skip"),
        "rejected_types": (("skip", "convert"), "skip",
                           "photos in formats the destination rejects (e.g. WebP, BMP): skip (listed in the report), "
                           "or convert to JPEG"),
        "dedupe": (("content", "exact", "off"), "content",
                   "skip items already on the destination: exact (same bytes), content (also same picture), off"),
        "existing": (list, ["/"], "where to look for copies already on the destination: [\"/\"] = the whole account "
                                  "(safest; listing takes a few minutes for tens of thousands of items), or folders or "
                                  "albums by name, e.g. [\"Family\", \"Old Imports/2023-05\"]; [] = only this "
                                  "project's folder"),
        "same_max": (int, 6, "content check: picture distance at or below this is the same photo"),
        "different_min": (int, 19, "content check: distance at or above this is a different photo; between = review"),
        "pair_window": (int, 60, "a clip pairs with a still taken within this many seconds"),
        "aspect_tolerance_pct": (int, 2, "a clip pairs only with a still of the same shape, within this percent"),
    },
    "organize": {
        "sources": (list, [], "folders or albums (by name) whose items are organized, e.g. [\"Uploads\"]"),
        "dates": (list, ["camera", "filename", "album"], "where capture dates come from, in order: camera (the "
                  "file's own date), filename, album (a date in the source album's name), upload (last resort)"),
        "mode": (("move", "collect"), "move", "move items into the dated albums, or collect them (copies stay in "
                                              "the source; use for albums an uploader app still writes to)"),
        "skip_newer_than_days": ("count", 0, "leave items uploaded within this many days alone (0 = none)"),
        "duplicates": (("park", "keep"), "park", "byte-identical extra copies: park in the duplicates album, or "
                                                "keep where they are"),
        "unassigned": (str, "Unassigned", "group name for items no [[organize.group]] rule matches"),
        "group": ("rules", [], "ordered rules naming a group, e.g. [[organize.group]] name = \"Phone\" "
                               "model = \"iPhone*\" (also: album, make, filename; case-insensitive globs)"),
    },
    "naming": {
        "scope": (list, [], "folders or albums (by name) whose album names are checked; [\"/\"] = the whole account"),
        "exclude": (list, [], "album names to leave alone (case-insensitive globs), e.g. [\"*Auto Upload*\"]"),
        "month": ("name_template", "{yyyy}-{mm} {subject}", "new name when the date has a month"),
        "year": ("name_template", "{yyyy} {subject}", "new name when only the year is known"),
        "day": ("name_template", "{yyyy}-{mm}-{dd} {subject}", "new name when keep_day is on and the day is known"),
        "keep_day": ((True, False), True, "keep the day when the old name has one (false drops it: less information)"),
        "min_confidence": (("high", "medium"), "high", "apply renames this sure without review; the rest are listed"),
        "date_from_photos": ((True, False), True, "date albums with no date in their name from a sample of photos"),
        "photo_sample": (int, 60, "photos sampled per undated album (a few random pages)"),
        "min_photos": (int, 5, "an undated album needs at least this many dated photos to be dated from them"),
        "max_spread_days": (int, 45, "photos spread over more days than this don't date an album"),
    },
    "run": {
        "move_batch_size": (int, 25, "items per batch move"),
        "workers": (int, 8, "parallel network workers"),
    },
}


class ConfigError(ValueError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__("invalid gp2sm.toml:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass
class ProjectConfig:
    root: str
    values: dict = field(default_factory=dict)   # section -> key -> value (defaults filled in)

    def get(self, section, key):
        return self.values[section][key]

    def path(self, relative):
        return relative if os.path.isabs(relative) else os.path.join(self.root, relative)

    @property
    def state_db(self):
        return self.path("state.db")

    @property
    def log_file(self):
        return self.path(os.path.join("logs", "gp2sm.log"))

    def tool_settings(self, credentials_file=None):
        """The flat settings dict the commands use."""
        a, d = self.values["albums"], self.values["destination"]
        return {
            "smugmug_config": credentials_file,
            "state_db": self.state_db,
            "log_file": self.log_file,
            "target_folder": d["folder"],
            "photo_album_template": a["photo"],
            "video_album_template": a["video"],
            "undated_photo_album": a["undated_photo"],
            "undated_video_album": a["undated_video"],
            "duplicates_album": a["duplicates"],
            "timezone": self.values["project"]["timezone"],
            "album_soft_cap": a["soft_cap"],
            "album_hard_cap": a["hard_cap"],
            "move_batch_size": self.values["run"]["move_batch_size"],
            "max_consecutive_failures": 5,
            "takeout": takeout_policies(self.values),
            "organize": organize_settings(self.values),
            "naming": section_settings("naming", self.values),
            "policy": list(self.values.get("policy", [])),
            "project_name": self.values["project"]["name"],
        }


def _check(section, key, kind, value, problems):
    where = f"[{section}] {key}"
    if kind is str:
        if not isinstance(value, str):
            problems.append(f"{where} must be a string, got {type(value).__name__}")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            problems.append(f"{where} must be a positive integer, got {value!r}")
    elif kind == "name_template":
        if not isinstance(value, str):
            problems.append(f"{where} must be a string")
            return
        fields = {f for _, f, _, _ in string.Formatter().parse(value) if f}
        unknown = fields - {"yyyy", "mm", "dd", "subject"}
        if unknown or "yyyy" not in fields or "subject" not in fields:
            problems.append(f"{where} must use {{yyyy}} and {{subject}} (and may use {{mm}}, {{dd}}), got {value!r}")
    elif kind == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{where} must be 0 or a positive integer, got {value!r}")
    elif kind == "rules":
        if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
            problems.append(f"{where} must be a list of tables ([[{section}.{key}]])")
            return
        for i, rule in enumerate(value, 1):
            unknown = set(rule) - set(RULE_KEYS)
            if unknown:
                problems.append(f"{where} rule {i} has unknown keys {sorted(unknown)}; allowed: {list(RULE_KEYS)}")
            if not isinstance(rule.get("name"), str) or not rule.get("name"):
                problems.append(f"{where} rule {i} needs a name")
            if not any(k in rule for k in RULE_KEYS[1:]):
                problems.append(f"{where} rule {i} has no conditions (album, make, model or filename)")
            if not all(isinstance(v, str) for v in rule.values()):
                problems.append(f"{where} rule {i}: values must be strings")
    elif kind is list:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append(f"{where} must be a list of strings, got {value!r}")
    elif kind == "template":
        if not isinstance(value, str):
            problems.append(f"{where} must be a string")
            return
        fields = {f for _, f, _, _ in string.Formatter().parse(value) if f}
        if "yyyy" not in fields:
            problems.append(f"{where} must contain {{yyyy}} (and usually {{mm}}), got {value!r}")
        unknown = fields - {"yyyy", "mm", "group"}
        if unknown:
            problems.append(f"{where} has unknown placeholders {sorted(unknown)}; "
                            "only {yyyy}, {mm} and {group} are allowed")
    elif isinstance(kind, tuple):
        if value not in kind:
            problems.append(f"{where} must be one of {list(kind)}, got {value!r}")
    elif kind == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            problems.append(f"{where} is not a known time zone: {value!r} (e.g. America/New_York, UTC)")


def check_policies(raw_policies, problems):
    """[[policy]] entries: scope (and optional exclude) globs plus neutral album settings."""
    if not isinstance(raw_policies, list) or not all(isinstance(p, dict) for p in raw_policies):
        problems.append("[[policy]] must be a list of tables")
        return []
    for n, pol in enumerate(raw_policies, 1):
        where = f"[[policy]] {n}"
        for key in ("scope", "exclude"):
            if key in pol and (not isinstance(pol[key], list) or not all(isinstance(v, str) for v in pol[key])):
                problems.append(f"{where}: {key} must be a list of folder/album globs")
        if not pol.get("scope"):
            problems.append(f"{where}: needs a scope, e.g. scope = [\"Family/*\"] ([\"*\"] = every album)")
        for key, value in pol.items():
            if key in ("scope", "exclude"):
                continue
            if key not in ALBUM_SETTING_VALUES:
                problems.append(f"{where}: unknown setting {key!r}; known: {sorted(ALBUM_SETTING_VALUES)}")
            elif value not in ALBUM_SETTING_VALUES[key] or isinstance(value, bool) != isinstance(
                    ALBUM_SETTING_VALUES[key][0], bool):
                problems.append(f"{where}: {key} must be one of {list(ALBUM_SETTING_VALUES[key])}, got {value!r}")
    return raw_policies


def validate(raw):
    """Return (values with defaults, problems)."""
    problems, values = [], {}
    for section in raw:
        if section not in SCHEMA and section != "policy":
            problems.append(f"unknown section [{section}]; expected one of {sorted(SCHEMA) + ['policy']}")
    values["policy"] = check_policies(raw.get("policy", []), problems)
    for section, keys in SCHEMA.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            problems.append(f"[{section}] must be a table")
            given = {}
        for key in given:
            if key not in keys:
                problems.append(f"unknown key [{section}] {key}; expected one of {sorted(keys)}")
        values[section] = {}
        for key, (kind, default, _) in keys.items():
            value = given.get(key, default)
            _check(section, key, kind, value, problems)
            values[section][key] = value
    a = values["albums"]
    if isinstance(a["soft_cap"], int) and isinstance(a["hard_cap"], int) and a["soft_cap"] > a["hard_cap"]:
        problems.append(f"[albums] soft_cap ({a['soft_cap']}) must not exceed hard_cap ({a['hard_cap']})")
    o = values["organize"]
    if isinstance(o["dates"], list):
        bad = [d for d in o["dates"] if d not in DATE_SOURCES]
        if bad:
            problems.append(f"[organize] dates has unknown sources {bad}; choose from {list(DATE_SOURCES)}")
    t = values["takeout"]
    if isinstance(t["same_max"], int) and isinstance(t["different_min"], int) and t["same_max"] >= t["different_min"]:
        problems.append(f"[takeout] same_max ({t['same_max']}) must be below different_min ({t['different_min']})")
    return values, problems


def section_settings(section, values=None):
    """A section's settings (defaults when no project config is in use)."""
    return dict(values[section]) if values else {k: v[1] for k, v in SCHEMA[section].items()}


def organize_settings(values=None):
    """The [organize] settings (defaults when no project config is in use)."""
    return dict(values["organize"]) if values else {k: v[1] for k, v in SCHEMA["organize"].items()}


def takeout_policies(values=None):
    """The [takeout] policy keys (defaults when no project config is in use)."""
    t = values["takeout"] if values else {k: v[1] for k, v in SCHEMA["takeout"].items()}
    return {k: v for k, v in t.items() if k not in ("archives", "index")}


def load(project_dir):
    path = os.path.join(project_dir, CONFIG_NAME)
    if not os.path.exists(path):
        raise ConfigError([f"no {CONFIG_NAME} in {project_dir} (create one with `gp2sm init`)"])
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{CONFIG_NAME} is not valid TOML: {e}"]) from e
    values, problems = validate(raw)
    if problems:
        raise ConfigError(problems)
    return ProjectConfig(root=os.path.abspath(project_dir), values=values)


def find_project(start=None):
    """The nearest folder (start or a parent) containing gp2sm.toml, else None."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, CONFIG_NAME)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
