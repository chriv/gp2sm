"""gp2sm.toml: schema, validation, and conversion to the settings the tools use.

Every problem is reported at once (unknown keys, wrong types, bad templates or time zones), so a user can fix
their config in one pass. Paths in the file are relative to the project folder.
"""

import os
import string
import sys
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 in CI
    import tomli as tomllib

CONFIG_NAME = "gp2sm.toml"

# section -> key -> (type, default, help). Types: str, int, list (of str), "template", "timezone",
# or a tuple of allowed strings (a choice).
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
        "photo": ("template", "Photos {yyyy}-{mm}", "dated album name; {yyyy} and {mm} come from the capture date"),
        "video": ("template", "Photos {yyyy}-{mm}", "dated album for videos (same as photo keeps them together)"),
        "undated_photo": (str, "Photos Undated", "album for photos with no confident date"),
        "undated_video": (str, "Videos Undated", "album for videos with no confident date"),
        "duplicates": (str, "Duplicates (review)", "album for byte-identical extra copies (moved, not deleted)"),
        "soft_cap": (int, 4000, "split an album into '- Part N' above this many items"),
        "hard_cap": (int, 5000, "never put more than this many items in one album"),
    },
    "consolidate": {
        "sources": (list, [], "substrings of album paths to consolidate from"),
        "legacy_dbs": (list, [], "optional legacy transfer databases (Google item lists) for dating"),
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
        "rejected_types": (("convert", "skip"), "convert",
                           "photos in formats the destination rejects (e.g. WebP, BMP): convert to JPEG, or skip"),
        "dedupe": (("content", "exact", "off"), "content",
                   "skip items already on the destination: exact (same bytes), content (also same picture), off"),
        "existing": (list, [], "folders or albums (by name, e.g. \"Family/2023-05\") to check for existing copies; "
                               "empty = this project's folder, [\"/\"] = the whole account"),
        "same_max": (int, 6, "content check: picture distance at or below this is the same photo"),
        "different_min": (int, 19, "content check: distance at or above this is a different photo; between = review"),
        "pair_window": (int, 60, "a clip pairs with a still taken within this many seconds"),
        "aspect_tolerance_pct": (int, 2, "a clip pairs only with a still of the same shape, within this percent"),
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
        """The flat settings dict the tool modules use (organize.consolidate.DEFAULTS keys)."""
        a, d = self.values["albums"], self.values["destination"]
        return {
            "smugmug_config": credentials_file,
            "state_db": self.state_db,
            "log_file": self.log_file,
            "legacy_dbs": [self.path(p) for p in self.values["consolidate"]["legacy_dbs"]],
            "source_album_patterns": list(self.values["consolidate"]["sources"]),
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
        }


def _check(section, key, kind, value, problems):
    where = f"[{section}] {key}"
    if kind is str:
        if not isinstance(value, str):
            problems.append(f"{where} must be a string, got {type(value).__name__}")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            problems.append(f"{where} must be a positive integer, got {value!r}")
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
        unknown = fields - {"yyyy", "mm"}
        if unknown:
            problems.append(f"{where} has unknown placeholders {sorted(unknown)}; only {{yyyy}} and {{mm}} are allowed")
    elif isinstance(kind, tuple):
        if value not in kind:
            problems.append(f"{where} must be one of {list(kind)}, got {value!r}")
    elif kind == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            problems.append(f"{where} is not a known time zone: {value!r} (e.g. America/New_York, UTC)")


def validate(raw):
    """Return (values with defaults, problems)."""
    problems, values = [], {}
    for section in raw:
        if section not in SCHEMA:
            problems.append(f"unknown section [{section}]; expected one of {sorted(SCHEMA)}")
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
    t = values["takeout"]
    if isinstance(t["same_max"], int) and isinstance(t["different_min"], int) and t["same_max"] >= t["different_min"]:
        problems.append(f"[takeout] same_max ({t['same_max']}) must be below different_min ({t['different_min']})")
    return values, problems


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
