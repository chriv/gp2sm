# gp2sm Roadmap

gp2sm moves photo libraries into SmugMug and keeps them organized:

- **Import from Google Takeout.** This includes Live Photos (JPEG still and motion clip side by side), HEIC conversion, and dates from Takeout metadata.
- **Organize existing SmugMug content.** It consolidates scattered or auto-upload albums into dated albums, removes byte-identical duplicates, and keeps state so every run can be resumed and undone.

The work is split into **stages that can be done one at a time**. Each stage has a goal, deliverables, the tests it adds, and exit criteria. Don't start a stage until the previous one's exit criteria are met.

## Principles (apply to every stage)

- **Safe by default.** Every command that writes is a dry run unless `--yes` is given. Deletions check the server's state immediately before acting. A write whose outcome is unknown (timeout/5xx) is never retried blindly; the server is checked first (see `docs/smugmug-api.md`). Nothing is deleted while a reversible alternative exists (e.g. park duplicates, then delete).
- **Easy to use.** One config file per project. `gp2sm init` writes a commented config. Every command explains what it will do and how to undo it. `gp2sm status` always answers "where am I?".
- **State, not memory.** Every decision and action is recorded in a per-project SQLite database with an append-only event log. Runs resume, and `verify` compares the server with the database.
- **Modular.** The SmugMug client, Takeout reading, media handling, planning and the CLI are separate packages. Planning logic is pure, with no I/O, so it can be unit tested.
- **Tests without personal data.** Fixtures are generated synthetically (images, videos, Takeout folder layouts, SmugMug responses). No real photos, filenames, IDs or account data in the repo. Network tests are opt-in and only touch a sandbox folder they delete afterwards.

## Current state (baseline)

Already working and tested (`gp2sm/`, `tests/`):

- `smugmug_client`: retries, ambiguous-write handling, paging, per-endpoint list keys, uploads, album sort.
- `consolidate`: inventory → match → plan → apply → verify, plus reconcile, undo and duplicate deletion. Used to consolidate about 24k items.
- `takeout_index` / `takeout_match`: stream-index archives without extracting them, and pair sidecars and Live Photo motion files.
- `content_match`: perceptual (dHash) matching between Takeout originals and existing SmugMug copies.
- `takeout_upload`:
  - plan / stage / upload / verify / remove
  - HEIC→JPEG with EXIF kept or filled in from Takeout metadata
  - a clip-timestamp pairing guard
  - filename sort, so each JPEG sits next to its clip

Known limitations that the stages below remove:

- HEIC conversion uses macOS `sips`.
- The CLI is a set of `python -m` modules.
- Configuration is JSON, with some project-specific assumptions (a legacy-database bridge).
- There's no packaging or CI.
- Legacy v1/v2 code is still in the tree.

---

## Part A: A tool others can use (do this first)

### Stage A0: Repo cleanup and packaging baseline

- **Goal:** a clean, installable repository with CI.
- **Deliverables:**
  - Tag the last commit that contains the legacy v1/v2 code (`legacy-v2`). Then remove from the default branch: `main.py`, `*_module.py`, `database_manager.py`, `constraints.md`, `systemprompt.md`, `nextplan.md`, `prompt_for_*.txt`, `smugmug_config.json.example`, `.gitlab-ci.yml`.
  - `pyproject.toml` with a `gp2sm` console entry point, dependency ranges, and `[dev]` extras (pytest, ruff).
  - GitHub Actions running ruff and pytest on Linux, macOS and Windows.
  - A synthetic fixture generator (`tests/fixtures/`): small JPEG/PNG/HEIC/MP4 files, a fake Takeout tree with metadata files, and recorded SmugMug JSON responses with all values replaced.
  - `tests/fakes/smugmug.py`: an in-memory SmugMug fake built from `tests/test_apply.py`, with all-or-nothing moves, ambiguous 504s, and HTTP 200 + `stat:"fail"`.
- **Tests:** the existing 50 tests move onto the shared fakes. A test checks that the fixture generator is deterministic.
- **Exit:** `pipx install .` gives a working `gp2sm --help`. CI is green on all three operating systems. No legacy files remain on the default branch.

### Stage A1: Modular core

- **Goal:** separate the existing code into stable modules without changing behavior.
- **Layout:**
  - `gp2sm/smugmug/` (client, models)
  - `gp2sm/takeout/` (index, sidecars, pairing)
  - `gp2sm/media/` (conversion, EXIF, clip times, perceptual hash)
  - `gp2sm/state/` (schema and **versioned migrations**)
  - `gp2sm/organize/` (pure planning)
  - `gp2sm/cli/`
- **Deliverables:**
  - Cross-platform HEIC conversion via `pillow-heif`, with a parity test proving EXIF (date, offset, camera, GPS) survives the same way it did with `sips`. `sips` stays as an optional backend.
  - A `User-Agent` that identifies the tool, and conservative default concurrency.
  - Schema migrations from the current state DB, so existing projects keep working.
- **Tests:** unit tests per module. Media tests use only generated files. A migration test upgrades a synthetic v1 database.
- **Exit:** all earlier behavior is reproduced on synthetic data. No module imports the CLI.

### Stage A2: Projects, config and command-line UX

- **Goal:** a new user can reach a reviewed dry-run plan by following the README, with no code reading.
- **Deliverables:**
  - **Projects:** `gp2sm init <project>` creates `<project>/gp2sm.toml` plus a state DB. Credentials are stored outside the project: the OS keyring, or a mode-0600 file in the user config directory. The SmugMug PIN sign-in is built in.
  - **One TOML schema** with validation and clear errors (unknown keys, bad templates, missing credentials), plus example configs for common setups.
  - **A consistent command set:** `status`, `plan`, `apply`, `verify`, `undo`, `report`.
  - **Behavior across commands:**
    - writes are dry runs unless `--yes`
    - progress display, and Ctrl-C finishes the current batch
    - a per-project lock file
- **Tests:**
  - config validation, using a table of good and bad configs
  - CLI smoke tests against the SmugMug fake
  - the lock file prevents concurrent runs
- **Exit:** someone following only the README completes `init` → `plan` (dry run) on a sandbox account.

### Stage A3: Generalized Takeout importer

- **Goal:** `gp2sm takeout …` works for anyone's Takeout, with no assumptions from an earlier tool.
- **Deliverables:**
  - `takeout index / plan / upload / verify` in the new command line. Each item is placed in an album based on its metadata `photoTakenTime`, using configurable templates.
  - **Deduplication against SmugMug content that's already there:** exact (MD5 for formats SmugMug stores byte-identically) and optional perceptual matching, with a "review" band that's never auto-decided.
  - **Policies in config:**
    - HEIC: convert or upload as is
    - Live Photo clips: pair next to the still, separate album, or skip
    - unpaired clips: target album
    - rejected types (WebP/BMP/ICO/very small videos): convert or skip
    - album naming and size caps
  - The old-database bridge moves to an optional `gp2sm.contrib.legacy_bridge`.
- **Tests:**
  - end to end on a synthetic Takeout tree against the fake, including:
    - pairs split across two archives
    - colliding `(N)` names
    - clips whose timestamps don't match their still
    - missing metadata dates
    - re-running after an interruption
  - an optional live test in the sandbox
- **Exit:** the synthetic end-to-end test passes, and a real small Takeout (a single album) imports cleanly with verify passing.

### Stage A4: Generalized organize (consolidation)

- **Goal:** rule-based reorganization of existing SmugMug albums.
- **Deliverables:**
  - **Config sections:**
    - `sources`: album or folder patterns
    - `dates`: an ordered list of sources (SmugMug `DateTimeCreated` → filename patterns → plugins → undated)
    - `grouping`: rules mapping source, camera make/model or other metadata to a name prefix, with a catch-all "unassigned" bucket
    - templates
    - video and undated policies
    - duplicates: park, then delete
  - **Re-runnable:** `skip_newer_than_days`, replanning that only adds new work, and a `collect` mode as an alternative to `move` for sources an uploader app still writes to.
- **Tests:** a planning matrix (date-source precedence, rule matching, cap splitting, keeping done items in place, collect vs move) and fake-backed apply/undo/verify.
- **Exit:** a dry-run plan on a real account looks right to its owner. A one-month pilot passes verify.

### Stage A5: Documentation and release

- **Deliverables:**
  - **README:** what it does, install, a 10-minute quickstart.
  - **docs/:**
    - getting a SmugMug API key
    - requesting a Google Takeout
    - config reference
    - the safety model, and how to undo
    - FAQ (Live Photos, HEIC, rejected types, album limits)
    - troubleshooting
    - `smugmug-api.md` (confirmed API behavior)
  - **Release:** a changelog, versioning, a PyPI release, and issue templates.
- **Exit:** a fresh-machine install and quickstart, done by someone who didn't write the code.

---

## Part B: Organizing phone auto-upload albums (uses Part A)

### Stage B0: How the uploader app behaves

- **Goal:** know how the SmugMug mobile app's automatic upload behaves before reorganizing anything it writes to.
- **Method:** on a test device, set up auto-upload to a new sandbox folder, then take a few photos, Live Photos and a short video. Watch:
  1. What gets uploaded for a Live Photo (still format, any motion clip, filenames, dates).
  2. Whether **moving** an uploaded photo to another album, or deleting it, makes the app upload it again. (Hypothesis: the app decides by a content hash.)
  3. Whether editing a photo on the device uploads a new copy.
- **Deliverables:** findings added to `docs/smugmug-api.md` (an "uploader app" section), and the choice of `move` vs `collect` for auto-upload sources.
- **Exit:** all three questions answered, and the test images removed.

### Stage B1: Dry-run plan for the auto-upload albums

- A separate project and config (so state is separate from any other project), with sources, person/device rules, and a date order that prefers each photo's own EXIF.
- Review the plan, the duplicate groups and the "unassigned" bucket. Adjust the rules until the plan looks right.

### Stage B2: Pilot, then full run, then routine

- A one-month pilot → verify → the full run → verify.
- Then run it periodically (e.g. monthly), skipping the most recent weeks, so new uploads are absorbed without disrupting the app.
