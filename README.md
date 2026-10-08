# gp2sm

[![CI](https://github.com/chriv/gp2sm/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chriv/gp2sm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status: beta](https://img.shields.io/badge/status-beta-orange)](CHANGELOG.md)
[![Platforms: Linux | macOS | Windows](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](.github/workflows/ci.yml)

Move a photo library into **SmugMug** and keep it organized. Every step is planned before it runs, recorded, checked against SmugMug afterwards, and undoable where SmugMug allows it.

> **Status: beta (3.0.0b3).** Used on a real 35,000-item migration (Google Photos → SmugMug, Live Photos included). Expect rough edges; please report them.

## What it does

- **Import a Google Takeout** (`gp2sm takeout …`). It reads the Takeout `.zip` or `.tgz` archives without extracting them. Before uploading anything, it checks what's already on SmugMug: the same file, the same picture saved differently (compared by appearance), or the same video. Only genuinely unclear cases are held back for you, as side-by-side files you drag into `same/` or `different/`. Everything new goes into month albums. Live Photos arrive as a JPEG with its video clip beside it under the same name. HEIC photos are converted to JPEG with their camera data kept, and missing capture dates are filled in from the Takeout.
- **Organize what's already on SmugMug** (`gp2sm organize …`). It gathers items from the albums you name (for example a phone's auto-upload album) into month albums. Each item is dated by the camera, the file name, or the album name, and grouped by rules you write (by person, device, file name). Items are moved, or "collected" so the originals stay where the uploader app put them. Identical copies are parked for review.
- **Tidy album names and settings** (`gp2sm albums …`). It proposes date-sortable names like `2019-06 Beach Trip`, reading the date from the name or from the photos inside, and renames only the display name, so links keep working. It also checks every album against the settings you want (privacy, downloads, sort order, …) and fixes the differences.
- **Report** (`gp2sm report`). It accounts for what happened to every file: already there, uploaded, held for review, skipped and why. The counts have to add up, and the report says so.

## Install

Install it from GitHub (Python 3.11 or newer):

```bash
pipx install git+https://github.com/chriv/gp2sm.git
gp2sm --help
```

From a checkout: `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`.

## Quickstart: Google Photos to SmugMug

1. **Request a Google Takeout first; it takes a while.** Google prepares the export in the background, and the more photos and videos you have, the longer it takes: from hours to several days for a large library. Select **Google Photos only** (choose "Deselect all" first): a Takeout of other Google services adds data gp2sm doesn't use and makes the export bigger and slower. Details: [docs/google-takeout.md](docs/google-takeout.md).
2. **While you wait, get a SmugMug API key** (once): see [docs/smugmug-api-key.md](docs/smugmug-api-key.md).
3. **Create a project** (a folder holding settings, progress and logs) and sign in:
   ```bash
   gp2sm init family-photos          # asks a few questions; writes family-photos/gp2sm.toml
   gp2sm auth smugmug                # API key and secret, then a 6-digit code from SmugMug
   cd family-photos
   ```
4. **When the Takeout is ready,** download every part into `family-photos/takeout/`. Don't extract them.
5. **Plan.** Nothing on SmugMug changes:
   ```bash
   gp2sm plan                        # indexes the archives, checks SmugMug, plans every upload
   gp2sm report                      # reports/report.html: what would happen to every file
   ```
   The plan skips anything already anywhere on your SmugMug account: the same file, the same picture saved differently, or the same video. To check only some folders (faster for a huge account), set `existing` under `[takeout]` in `gp2sm.toml`. If the plan holds anything for review, run `gp2sm takeout review`, sort the pairs in `review/`, then run `gp2sm takeout review --read` and `gp2sm plan` again.
6. **Upload and check:**
   ```bash
   gp2sm apply                       # prepares the files locally, then shows what it would upload
   gp2sm apply --yes                 # uploads (stop any time with Ctrl-C; run it again to continue)
   gp2sm verify                      # confirms every upload on SmugMug
   gp2sm report
   ```

Every setting (album names, HEIC handling, Live Photo clips, what to skip) is in `gp2sm.toml`, with its help text. See [docs/config.md](docs/config.md).

## Other tasks

Each task works in a project and follows the same pattern: plan, look at the report, apply with `--yes`, verify, and undo if needed. All of them are dry runs until you add `--yes`.

**Organize photos already on SmugMug**, e.g. a phone's auto-upload album, into month albums. In `gp2sm.toml`, set `sources` under `[organize]` to the albums or folders to organize, and optionally add grouping rules (`[[organize.group]]`) and `mode = "collect"` to leave the originals in place. Then:
```bash
gp2sm organize inventory && gp2sm organize plan && gp2sm organize report
gp2sm organize apply --yes && gp2sm organize verify
gp2sm organize undo "Photos 2024-05" --yes      # if you change your mind about an album
```

**Give albums date-sortable names** like `2019-06 Beach Trip` (display names only; links keep working). Set `scope` under `[naming]` (e.g. `["/"]` for every album), then:
```bash
gp2sm albums inventory && gp2sm albums plan && gp2sm albums report
gp2sm albums approve --name "Cotton Pickin 2011"  # proposals marked for review need your OK
gp2sm albums apply --yes && gp2sm albums verify
```

**Keep album settings consistent** (privacy, downloads, sort order, …). Add `[[policy]]` entries to `gp2sm.toml` (a commented example is in every new one), then:
```bash
gp2sm albums audit && gp2sm albums report         # what differs from your policy
gp2sm albums fix --yes && gp2sm albums verify
gp2sm albums undo --yes                           # restores the previous names and settings
```

## Safety

Anything that changes SmugMug is a dry run unless you add `--yes`. Deletions are separate commands that check the server first. Every change is recorded before it's sent. Outcomes that can't be known (a timeout, a server error) are checked against SmugMug before anything is retried. One command at a time can change a project. See [docs/safety.md](docs/safety.md) for what each command can undo.

## Extending: photo services are plugins

gp2sm talks to photo services only through two interfaces in `gp2sm/services/base.py`: a **source** (where items come from, such as a Google Takeout) and a **destination** (where they go, such as SmugMug). A destination also declares its capabilities: which formats it converts or rejects, album size limits, whether items can sit in several albums, and which album settings it supports. The core reads those instead of hard-coding one service. The built-in Google Takeout source and SmugMug destination register like any third-party plugin, through the `gp2sm.services` entry-point group. `gp2sm services` lists what's installed, and shared contract tests (`tests/contracts/`) check that a new service behaves the way the core expects.

## Documentation

- [docs/smugmug-api-key.md](docs/smugmug-api-key.md): getting a SmugMug API key
- [docs/google-takeout.md](docs/google-takeout.md): requesting and downloading a Google Takeout
- [docs/config.md](docs/config.md): every setting in `gp2sm.toml`
- [docs/safety.md](docs/safety.md): dry runs, verification, undo, and what can't be undone
- [docs/faq.md](docs/faq.md): Live Photos, HEIC, rejected file types, album limits, uploader apps
- [docs/troubleshooting.md](docs/troubleshooting.md)
- [docs/smugmug-api.md](docs/smugmug-api.md): SmugMug API behavior confirmed by tests, including undocumented traps (for developers)
- [docs/ROADMAP.md](docs/ROADMAP.md): how the tool was built, and what's next
- [CHANGELOG.md](CHANGELOG.md)

## License

[MIT](LICENSE). Not affiliated with Google or SmugMug.
