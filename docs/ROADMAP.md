# gp2sm Roadmap

gp2sm moves photo libraries into SmugMug and keeps them organized:

- **Import from Google Takeout.** This includes Live Photos (JPEG still and motion clip side by side), HEIC conversion, and dates from Takeout metadata.
- **Organize existing SmugMug content.** It consolidates scattered or auto-upload albums into dated albums, removes byte-identical duplicates, and keeps state so every run can be resumed and undone.
- **Manage albums.** It standardizes album names (e.g. `YYYY-MM Subject`), audits album settings against preferred policies (downloads, original sizes, watermarks, privacy, sort order), and fixes drift in bulk, with undo.

Part A builds the tool; Part B applies it. The work is split into **stages that can be done one at a time**. Each stage has a goal, deliverables, the tests it adds, and exit criteria. Don't start a stage until the previous one's exit criteria are met.

## Principles (apply to every stage)

- **Safe by default.** Every command that writes is a dry run unless `--yes` is given. Reversible actions (moves, uploads, dating, pairing) can be automated freely as long as they're state-tracked and verified. Deletion is gated, with one automatable exception: an album the tool itself created, that the server confirms is empty, and that has nothing planned for it. Deletions check the server's state immediately before acting. A write whose outcome is unknown (timeout/5xx) is never retried blindly; the server is checked first (see `docs/smugmug-api.md`). Nothing is deleted while a reversible alternative exists (e.g. park duplicates, then delete).
- **Easy to use.** One config file per project. `gp2sm init` writes a commented config. Every command explains what it will do and how to undo it. `gp2sm status` always answers "where am I?".
- **State, not memory.** Every decision and action is recorded in a per-project SQLite database with an append-only event log. Runs resume, and `verify` compares the server with the database.
- **Modular.** The SmugMug client, Takeout reading, media handling, planning and the CLI are separate packages. Planning logic is pure, with no I/O, so it can be unit tested.
- **Tests without personal data.** Fixtures are generated synthetically (images, videos, Takeout folder layouts, SmugMug responses). No real photos, filenames, IDs or account data in the repo. Network tests are opt-in and only touch a sandbox folder they delete afterwards.

## Current state (baseline)

Already working and tested (`gp2sm/`, `tests/`):

- `smugmug/client.py`: retries, ambiguous-write handling, paging, per-endpoint list keys, uploads, album sort.
- `organize/consolidate.py`: inventory → match → plan → apply → verify, plus reconcile, undo and duplicate deletion. Used to consolidate about 24k items.
- `takeout/index.py` / `takeout/match.py`: stream-index archives without extracting them, and pair sidecars and Live Photo motion files.
- `organize/content_match.py`: perceptual (dHash) matching between Takeout originals and existing SmugMug copies.
- `takeout/upload.py`:
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

## Part A: Building the tool (do this first)

Everything that makes gp2sm a better tool: import, organize, album naming and settings management, and release.

### Stage A0: Repo cleanup and packaging baseline. ✅ Done (2026-10-06)

- **Goal:** a clean, installable repository with CI.
- **Deliverables:**
  - Tag the last commit that contains the legacy v1/v2 code (`legacy-v2`). Then remove from the default branch: `main.py`, `*_module.py`, `database_manager.py`, `constraints.md`, `systemprompt.md`, `nextplan.md`, `prompt_for_*.txt`, `smugmug_config.json.example`, `.gitlab-ci.yml`.
  - `pyproject.toml` with a `gp2sm` console entry point, dependency ranges, and `[dev]` extras (pytest, ruff).
  - GitHub Actions running ruff and pytest on Linux, macOS and Windows.
  - A synthetic fixture generator (`tests/fixtures/`): small JPEG/PNG/HEIC/MP4 files, a fake Takeout tree with metadata files, and recorded SmugMug JSON responses with all values replaced.
  - `tests/fakes/smugmug.py`: an in-memory SmugMug fake built from `tests/test_apply.py`, with all-or-nothing moves, ambiguous 504s, and HTTP 200 + `stat:"fail"`.
- **Tests:** the existing 50 tests move onto the shared fakes. A test checks that the fixture generator is deterministic.
- **Exit:** `pipx install .` gives a working `gp2sm --help`. CI is green on all three operating systems. No legacy files remain on the default branch.

### Stage A1: Modular core and service plugins. ✅ Done (2026-10-06)

- **Goal:** separate the existing code into stable modules without changing behavior, and make photo services pluggable on both ends (sources and destinations), so services other than Google Takeout and SmugMug can be added later without a redesign.
- **Service layer:**
  - **Neutral models:** `MediaItem` (id, name, kind, capture time, size, hash, dimensions, duration, opaque service ref, raw extras) and `Collection` (album/folder).
  - **Interfaces:**
    - `PhotoSource`: iterate items, open their bytes, metadata
    - `PhotoDestination`: list/create collections, upload, move, delete, locate an item, read/set collection properties
  - **Capability flags declared by each service,** read by the core instead of hard-coding one service's behavior. Examples: atomic batch moves, stores original bytes per type, server-side format conversion, max items per collection, rejected media rules, whether a write can succeed despite a timeout.
  - **Adapters own everything service-specific:** SmugMug (URLs, field names, quirks such as HEIC→JPG renaming and tiny-video rejection) and Google Takeout (archive layout, metadata files, Live Photo pairing). Core modules never import an adapter directly.
  - **Plugin discovery** via Python entry points (`gp2sm.services`), so `pip install gp2sm-<service>` can add a service.
  - **Contract tests:** one shared conformance suite that every adapter must pass against its own fake.
- **State:** a neutral schema (`service` + opaque item/collection refs, service extras as JSON) with a migration from the current SmugMug-/Google-shaped columns.
- **Housekeeping:** the local probe scripts (`probes/`, gitignored) still import the removed legacy SmugMug module for auth. Port them to the client/adapter before reusing them.
- **Sub-stages** (one commit each; CI must pass before the next):
  - A1.1 neutral item/album records, `PhotoSource`/`PhotoDestination` interfaces, capability flags
  - A1.2 SmugMug adapter behind `PhotoDestination` + a shared contract test suite for destinations
  - A1.3 Google Takeout behind `PhotoSource`
  - A1.4 package layout (`smugmug/`, `takeout/`, `media/`, `state/`, `organize/`, `cli/`), behavior unchanged
  - A1.5 cross-platform HEIC conversion (`pillow-heif`) with an EXIF parity test
  - A1.6 entry-point plugin discovery + versioned state migrations
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

### Stage A2: Projects, config and command-line UX. ✅ Done (2026-10-06)

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
- **Sub-stages** (one commit each; CI passed before the next):
  - A2.1 project config (`gp2sm.toml`) with schema validation
  - A2.2 per-user credential store (mode-0600 files in the user config dir) and `gp2sm auth` with the built-in SmugMug PIN sign-in
  - A2.3 `gp2sm init` writes a commented `gp2sm.toml`
  - A2.4 every command resolves the project (or legacy JSON), takes paths and credentials from it, and takes a per-project lock for destination writes; `gp2sm status`
  - A2.5 dry run unless `--yes` for every destination write, progress display, consistent Ctrl-C handling and run bookkeeping; example configs; `gp2sm services`
- **Moved to A4:** the single top-level verb set (`gp2sm plan|apply|verify|undo|report` across pipelines). It needs the generalized pipeline, so for now each pipeline keeps its own verbs (`gp2sm consolidate plan`, `gp2sm takeout-upload upload`, …), all following the same rules. The OS keyring is optional for later; the 0600 file store covers it.

### Stage A3: Generalized Takeout importer. ✅ Done (2026-10-07)

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
  - **Automatic decisions, with a narrow review band.** People only see what the checks below can't decide.
    - **Name collisions** (a Takeout photo vs. a same-named photo already in the target album) are settled by a content check, not held by default:
      - dHash ≤ 6 → same photo, skip
      - ≥ 19 → different photo, upload
      - 7–18 → review

      (Validated 2026-10-06: 16 of 16 human-judged collisions scored 21–31 and were all "different".)
    - **Live Photo clip pairing** uses each clip's own capture time (Apple `creationdate`, falling back to `mvhd`) and its aspect ratio, searched across *all* stills, not just the same-named one:
      - within 60 s with the same aspect ratio → paired automatically (closest first, one-to-one)
      - no candidate → unsorted-videos album

      (Validated: 143 of 149 held clips re-paired, 135 within 1 s. This caught clips that a name-based pairing had swapped between items, e.g. `RenderedImage` and reused `IMG_####` names.)
    - **Burst frames** (near-identical shots, e.g. `lp_image`) are paired one-to-one by closeness, ignoring the margin rule, because only the moment/month matters.
  - **Review output, when needed, is flat and drag-sortable:** `<check>/` holds same-named files side by side, with `good/`/`bad/` (or `same/`/`different/`) subfolders, and decisions are read back from where files end up.
- **Tests:**
  - end to end on a synthetic Takeout tree against the fake, including:
    - pairs split across two archives
    - colliding `(N)` names
    - clips whose timestamps don't match their still
    - missing metadata dates
    - re-running after an interruption
  - an optional live test in the sandbox
- **Exit:** the synthetic end-to-end test passes, and a one-month slice cut from a real Takeout (a Takeout always contains the whole service) plans correctly against that month's existing album.
- **Sub-stages** (one commit each; CI must pass before the next):
  - A3.1 archives in both export formats (`.zip`, `.tgz`) behind one reader; the index records each file's own dimensions, duration and capture time
  - A3.2 import policies in `gp2sm.toml`
  - A3.3 the service-neutral importer and `gp2sm takeout …`:
    - destination inventory (`list_folder_albums`, by folder names)
    - dedupe with a review band and flat drag-to-sort review folders
    - the pure planner, with clips paired by time and shape across all stills
    - stage, upload and verify reused
    - end-to-end synthetic test
  - A3.4 the old-database bridge moves to `gp2sm.contrib.legacy_bridge` (Takeout side; `consolidate`'s legacy import follows in A4)
  - A3.5 live sandbox test, then a one-month slice of a real Takeout
- **Known gap, deliberately left out of A3 (from the one-month check, 2026-10-07):** affects roughly 0.5% of clips at most (about 60 of 10,534 in the first real library), and only when an unpaired clip is already on the destination through some other route; the worst case is a duplicate clip, never a lost one. a Live Photo clip that pairs with no still is uploaded without first checking whether it's already on the destination. Same name and duration are too weak for clips, since bursts share names and most clips run 1–3 s. Fix: match on the destination copy's capture time, fetched only for same-named clip candidates.

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
- **Sub-stages** (one commit each; CI must pass before the next):
  - A4.1 rules and config: `[organize]` (sources, date chain, `[[organize.group]]`, mode, `skip_newer_than_days`, duplicates), `{group}` in album templates
  - A4.2 `gp2sm organize inventory|plan|report|apply|verify|undo` on those rules; legacy dating becomes a `contrib` plugin
  - A4.3 collect mode (after a sandbox probe of collect/remove semantics)
  - A4.4 top-level verbs (`gp2sm plan|apply|verify|undo|report`) dispatching to the project's pipeline
  - A4.5 real check: a dry-run plan on one small album, then a one-month pilot the owner approves


**Album management:** bulk audit and repair of album **names** and **settings**, using the same model: inventory → plan (dry run, readable report) → apply with `--yes` → verify. Every change records the album's previous values, so `undo` restores them.

### Stage A5: Probe what can be changed (album settings)

- **Goal:** confirm which album and folder properties the API can read and change, before building on them.
- **Method:** sandbox-album probes. For each property, read, `PATCH`, read back, then restore. Properties include:
  - `Name`/`UrlName`
  - `Privacy`, `SmugSearchable`/`WorldSearchable`
  - `AllowDownloads`, `LargestSize`/original-size access
  - `Watermark`/`Watermarked`
  - `Share`, `Comments`, `CanRank`
  - `SortMethod`/`SortDirection`
  - `Date`, `Description`/`Keywords`

  Also check whether a `UrlName` change breaks existing links, and how URL collisions are reported.
- **Deliverables:** an "album settings" section in `docs/smugmug-api.md` listing each property's allowed values, account-level requirements (e.g. Pro-only features) and quirks.
- **Exit:** every property the later stages use is confirmed.

### Stage A6: Naming conventions

- **Goal:** consistent, date-sortable album names (default template `{yyyy}-{mm} {subject}`, configurable).
- **Deliverables:**
  - **Name parsing:** a date parser for common patterns — `Subject MM-YYYY`, `MM-YYYY Subject`, `YYYY-MM-DD Subject`, `Subject YYYY`, `Month YYYY Subject`, two-digit years, mixed separators. Each result carries a confidence level.
  - **Names with no date:** derive one from the album's images (median capture date, falling back to upload dates). Flag low-spread vs. wide-spread date ranges, e.g. an album covering years.
  - **Scope rules:** which folders/albums to include or exclude, whether to keep day precision, and how to clean up the subject (trim, title case off by default).
  - **Report:** a proposed-rename table (old → new, date source, confidence). Only proposals at or above the confidence threshold are auto-applied; the rest are listed for review. `UrlName` follows the new name only if configured, because it changes album links.
- **Tests:** a large parser table (ambiguous `03-04`, two-digit years, names that contain numbers but aren't dates), image-date fallback, collision handling. All with synthetic names.
- **Exit:** a dry-run report on a real account looks right. A small batch rename passes verify and can be undone.

### Stage A7: Settings policy (audit and bulk fix)

- **Goal:** state the preferred album settings once, and find and fix drift.
- **Deliverables:**
  - **Config `policies`:** an ordered list of scope → desired settings. For example: public albums allow downloads up to a given size with no originals; private family albums allow originals; watermark on for public; sort by date taken.
  - **Commands:**
    - `gp2sm albums audit`: a drift report (CSV/HTML) per album and per setting
    - `gp2sm albums fix`: idempotent `PATCH` calls in batches, recording before-values
    - `gp2sm albums undo`: restores those recorded values
  - **Other checks** in the same audit:
    - empty albums
    - albums near or over the item cap
    - albums whose sort method breaks Live Photo pairing
    - albums whose name date disagrees with their contents
- **Tests:** policy precedence and scope matching, drift computation, apply/undo against the SmugMug fake.
- **Exit:** an audit on a real account, then a fix on one scope, then verify (re-audit shows no drift), then undo works on that scope.

### Stage A8: Results reporting

- **Goal:** `gp2sm report` turns a project's state database into a clear, trustworthy account of what happened. Results are the point of the tool, so they get first-class output.
- **Deliverables:**
  - **Outcome summary:** items on the destination, how many are in dated vs. undated collections, how many are verified on the server, collections created.
  - **Accounting, one row per file type:** every source file counted exactly once, by outcome:
    - already present (earlier copy)
    - uploaded and sorted
    - uploaded but unsorted
    - represented by a near-identical twin
    - rejected by the destination
    - excluded by the user
    - left behind, with reasons

    Totals must add up, and the report checks that they do.
  - **Evidence:** how each item was dated or matched (hash, name + time, content, own timestamp, video shape, …), clip-pairing time gaps, and the items left unresolved with their evidence.
  - **Verification status:** verified / unverified / missing, and when each was last checked.
  - **Activity log:** runs, commands, and summaries of the event log.
  - **Anticipated vs. final:** a report generated while work is in flight states what it assumes (e.g. replacements verified, old copies still being removed).
- **Output formats:** Markdown (readable), self-contained HTML, and machine-readable JSON/CSV. Reports are written into the project's own (private) data directory, since they name real albums and files.
- **Prototype:** the per-project report and file-type accounting built during the first real migration. Generalize them and remove anything account-specific.
- **Tests:** a synthetic state DB with known outcomes produces the expected totals. The accounting invariant (each file counted once, rows sum to totals) is tested directly.
- **Exit:** a report on a real project that its owner finds complete and accurate without asking follow-up questions.

### Stage A9: Documentation and release

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

## Part B: Applying the tool to a real account (uses Part A)

Each step below is its own project (separate config and state), so results stay separate and each can be re-run.

### Stage B0: How the uploader app behaves

- **Goal:** know how the SmugMug mobile app's automatic upload behaves before reorganizing anything it writes to.
- **Method:** on a test device, set up auto-upload to a new sandbox folder, then take a few photos, Live Photos and a short video. Watch:
  1. What gets uploaded for a Live Photo (still format, any motion clip, filenames, dates).
  2. Whether **moving** an uploaded photo to another album, or deleting it, makes the app upload it again. (Hypothesis: the app decides by a content hash.)
  3. Whether editing a photo on the device uploads a new copy.
- **Deliverables:** findings added to `docs/smugmug-api.md` (an "uploader app" section), and the choice of `move` vs `collect` for auto-upload sources.
- **Exit:** all three questions answered, and the test images removed.

### Stage B1: Phone auto-upload albums

- Organize (A4) the uploader-app folders: sources, person/device rules, and a date order that prefers each photo's own EXIF.
- Dry-run plan → review duplicates and the "unassigned" bucket → one-month pilot → full run → verify.

### Stage B2: Account-wide naming and settings cleanup

- Naming (A6): propose `YYYY-MM Subject` renames across chosen folders, auto-apply the confident ones, review the rest.
- Settings policy (A7): define the preferred settings per scope, audit drift, fix it in bulk, then re-audit.

### Stage B3: Routine drift checks

- Run the auto-upload organize periodically, skipping the most recent weeks.
- Run the naming and settings audits periodically as a report, applying fixes after a quick review of the drift.
