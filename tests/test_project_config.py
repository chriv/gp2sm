import glob
import os
import shutil

import pytest

from gp2sm.project.config import ConfigError, find_project, load, validate

GOOD = """
[project]
name = "test"
timezone = "America/New_York"

[destination]
folder = "My Imports"

[albums]
photo = "Imports {yyyy}-{mm}"
video = "Imports {yyyy}-{mm}"

[organize]
sources = ["Old-Import", "Auto-Upload"]
"""


def write(tmp_path, text):
    (tmp_path / "gp2sm.toml").write_text(text)
    return tmp_path


def test_good_config_loads_with_defaults(tmp_path):
    cfg = load(write(tmp_path, GOOD))
    assert cfg.get("albums", "photo") == "Imports {yyyy}-{mm}"
    assert cfg.get("albums", "soft_cap") == 4000                       # default filled in
    s = cfg.tool_settings(credentials_file="/creds/smugmug.json")
    assert s["target_folder"] == "My Imports" and s["organize"]["sources"] == ["Old-Import", "Auto-Upload"]
    assert s["state_db"] == str(tmp_path / "state.db") and s["smugmug_config"] == "/creds/smugmug.json"


@pytest.mark.parametrize("snippet, message", [
    ("[albums]\nphoto = 'Imports {mm}'", "must contain {yyyy}"),
    ("[albums]\nphoto = 'Imports {yyyy}-{day}'", "unknown placeholders"),
    ("[project]\ntimezone = 'Mars/Olympus'", "not a known time zone"),
    ("[albums]\nsoft_cap = 0", "positive integer"),
    ("[albums]\nsoft_cap = 6000\nhard_cap = 5000", "must not exceed hard_cap"),
    ("[organize]\nsources = 'Old-Import'", "list of strings"),
    ("[albums]\nphot = 'x {yyyy}'", "unknown key [albums] phot"),
    ("[albumz]\nphoto = 'x {yyyy}'", "unknown section [albumz]"),
    ("[run]\nworkers = true", "positive integer"),
])
def test_bad_configs_explain_the_problem(snippet, message):
    import sys
    toml = __import__("tomllib") if sys.version_info >= (3, 11) else __import__("tomli")
    _, problems = validate(toml.loads(snippet))
    assert any(message in p for p in problems), problems


def test_all_problems_reported_at_once(tmp_path):
    write(tmp_path, "[albums]\nphoto = 'x'\nsoft_cap = -1\n[project]\ntimezone = 'Nowhere'\n")
    with pytest.raises(ConfigError) as e:
        load(tmp_path)
    assert len(e.value.problems) == 3


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigError, match="gp2sm init"):
        load(tmp_path)
    write(tmp_path, "this is = = not toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(tmp_path)


def test_find_project_walks_up(tmp_path):
    write(tmp_path, GOOD)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project(str(sub)) == str(tmp_path)


def test_tool_settings_cover_every_setting_the_commands_use(tmp_path):
    settings = load(write(tmp_path, GOOD)).tool_settings()
    used = {"smugmug_config", "state_db", "log_file", "target_folder", "photo_album_template", "video_album_template",
            "undated_photo_album", "undated_video_album", "duplicates_album", "timezone", "album_soft_cap",
            "album_hard_cap", "move_batch_size", "max_consecutive_failures", "takeout", "organize", "naming"}
    assert used <= set(settings)
    assert settings["organize"]["sources"] == ["Old-Import", "Auto-Upload"]


@pytest.mark.parametrize("example", sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "examples", "*.toml"))))
def test_example_configs_are_valid(example, tmp_path):
    shutil.copy(example, tmp_path / "gp2sm.toml")
    load(str(tmp_path))


def test_takeout_policies_validate_choices_and_thresholds():
    _, problems = validate({"takeout": {"heic": "maybe", "same_max": 20, "different_min": 19}})
    assert any("[takeout] heic must be one of ['convert', 'keep']" in p for p in problems)
    assert any("same_max (20) must be below different_min (19)" in p for p in problems)
    values, problems = validate({"takeout": {"live_clips": "separate", "dedupe": "exact"}})
    assert problems == []
    assert values["takeout"]["live_clips"] == "separate" and values["takeout"]["unpaired_clips"] == "dated"


def test_tool_settings_carry_takeout_policies(tmp_path):
    (tmp_path / "gp2sm.toml").write_text('[takeout]\nheic = "keep"\n')
    policies = load(str(tmp_path)).tool_settings()["takeout"]
    assert policies["heic"] == "keep" and policies["pair_window"] == 60
    assert "archives" not in policies and "index" not in policies


def test_organize_section_validates_rules_dates_and_group_placeholder(tmp_path):
    _, problems = validate({"organize": {"dates": ["camera", "horoscope"],
                                         "group": [{"name": "A", "colour": "red"}, {"model": "x"}],
                                         "skip_newer_than_days": -1},
                            "albums": {"photo": "{group} {yyyy}-{mm}"}})
    text = "\n".join(problems)
    assert "unknown sources ['horoscope']" in text
    assert "rule 1 has unknown keys ['colour']" in text and "rule 1 has no conditions" in text
    assert "rule 2 needs a name" in text
    assert "skip_newer_than_days must be 0 or a positive integer" in text
    assert "placeholders" not in text                     # {group} is allowed
    (tmp_path / "gp2sm.toml").write_text('[albums]\nphoto = "{group} {yyyy}-{mm}"\n\n[organize]\nmode = "collect"\n'
                                         '[[organize.group]]\nname = "Phone"\nmodel = "iPhone*"\n')
    org = load(str(tmp_path)).tool_settings()["organize"]
    assert org["mode"] == "collect" and org["group"] == [{"name": "Phone", "model": "iPhone*"}]


def test_rules_render_as_valid_toml(tmp_path):
    from gp2sm.project.init import render
    text = render({"organize": {"group": [{"name": "Phone", "model": "iPhone*"}]}})
    (tmp_path / "gp2sm.toml").write_text(text)
    assert load(str(tmp_path)).get("organize", "group") == [{"name": "Phone", "model": "iPhone*"}]


def test_naming_section_defaults_and_template_check():
    values, problems = validate({"naming": {"month": "{subject} {mm}", "keep_day": True}})
    assert any("[naming] month must use {yyyy} and {subject}" in p for p in problems)
    values, problems = validate({})
    assert problems == [] and values["naming"]["min_confidence"] == "high" and values["naming"]["keep_day"] is True
