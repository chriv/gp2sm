# Changelog

All notable changes to gp2sm. Versions follow [semantic versioning](https://semver.org): after 3.0, new features come in minor releases and fixes in patch releases.

## 3.0.0b1 (beta)

A rewrite of the earlier v1/v2 script (kept at the git tag `legacy-v2`) as an installable tool, built and tested on a real 35,000-item migration.

- **Projects:** `gp2sm init` creates a folder with a commented `gp2sm.toml`. `gp2sm auth smugmug` signs in once and stores credentials per user, outside the project. `gp2sm status` and `gp2sm services` show where things stand.
- **Google Takeout import** (`gp2sm takeout …`):
  - reads `.zip` and `.tgz` archives without extracting them
  - checks what's already on SmugMug: the same file, the same picture re-encoded (perceptual hash), or the same video shape
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
