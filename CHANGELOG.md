# Changelog

All notable changes to gp2sm. Versions follow [semantic versioning](https://semver.org): after 3.0, new features come in minor releases and fixes in patch releases.

## 3.0.0b3 (beta)

Found and fixed while organizing a real account (auto-upload albums, a folder merge, renames, settings policies).

- **Fixed:**
  - `organize verify` reported every existing target album as wrong, because of the items that were already there (#6).
  - The `album` date source didn't read month galleries named like `2016-08` (#5).
  - Album renames kept words left dangling by the date ("Portraits taken on …") and mangled year ranges (#7).
  - The settings audit blamed a folder for privacy when the album itself was private (#9).
  - `albums verify` and `undo` got confused when gp2sm changed the same setting twice: undo restored the intermediate values (#10).
- **New:**
  - Album templates can put albums in subfolders, e.g. `{yyyy}/{yyyy}-{mm}` for year folders (#4). SmugMug now names new folders' and albums' web addresses itself (`2016-08`, not `A-2016-08`).
  - A year-range album (a school year, `2019/2020`) is dated from its photos when they're one tight cluster inside the range, and the range is kept (#8).
  - `albums approve --name … --as "NEW NAME"`: approve one reviewed rename under a name you choose.
  - `albums inventory --name GLOB`.
  - Checks (`plan`, `verify`, `albums audit`) exit with code 3 when they find something for a person to look at, so a script or scheduler can tell drift from a clean run.
- **State schema v2:** older project databases upgrade automatically.
- **Documented:** a name with a full date is trusted without checking its photos; album listings lag behind deletions; `moveimages` sometimes returns 500/504.

## 3.0.0b2 (beta)

- **Fixed:** listing a SmugMug album lost each item's metadata after the first page, so most items looked undated to organize (#1). SmugMug's next-page link drops `_expand`, so the original parameters are now resent on every page.
- **Safer:** `organize delete-duplicates` refuses when a copy is also in another album, because deleting it would remove it there too (#2).

## 3.0.0b1 (beta)

A rewrite of the earlier v1/v2 script (kept at the git tag `legacy-v2`) as an installable tool, built and tested on a real 35,000-item migration.

Licensed under MIT (earlier versions were released under the Unlicense).

- **Projects:** `gp2sm init` creates a folder with a commented `gp2sm.toml`. `gp2sm auth smugmug` signs in once and stores credentials per user, outside the project. `gp2sm status` and `gp2sm services` show where things stand.
- **Google Takeout import** (`gp2sm takeout …`):
  - reads `.zip` and `.tgz` archives without extracting them
  - checks what's already anywhere on the SmugMug account (by default; `[takeout] existing` can narrow it): the same file, the same picture re-encoded (perceptual hash), or the same video shape
  - a review folder for unclear cases
  - month albums
  - Live Photo clips paired with their still by capture time and shape
  - HEIC converted to JPEG with EXIF kept
  - policies for HEIC, Live Photo clips and rejected formats
- **Organize** (`gp2sm organize …`):
  - gathers items already on SmugMug into dated, grouped albums, with dates from the camera, file names or album names
  - grouping rules
  - move or collect
  - identical copies parked, and deleted only after their kept copy is verified on the server
- **Album names and settings** (`gp2sm albums …`):
  - date-sortable display-name renames, with links unchanged
  - a settings policy with audit, fix, verify and undo
- **Reports** (`gp2sm report`): every source file accounted for exactly once, with Markdown, HTML, JSON and CSV output.
- **Safety:**
  - every change that touches SmugMug is a dry run unless `--yes`
  - changes are recorded before they're sent, and uncertain outcomes are checked against the server
  - Ctrl-C stops cleanly
  - one command at a time per project
  - `verify` and `undo`
- **Services:** photo sources and destinations are plugins (entry point group `gp2sm.services`), checked against shared contract tests.
