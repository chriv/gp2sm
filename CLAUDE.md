# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## State of the repo

- The legacy v1/v2 script was removed. It's preserved at the git tag `legacy-v2`, and none of its code is trusted (core parts never worked). All work is in the `gp2sm/` package.
- `docs/ROADMAP.md` is the staged plan (do one stage at a time). `docs/smugmug-api.md` records SmugMug behavior **confirmed by probes**. Check it before relying on any SmugMug endpoint, and add anything newly confirmed.
- The repo is meant to become public. Keep personal names, account details and real album names out of tracked files. They belong in gitignored config (`data/`, `*.json`). The one intended exception is the owner's name as copyright holder in `LICENSE` (MIT).

## Commands

```bash
.venv/bin/pip install -e '.[dev]'                        # install (editable) with pytest + ruff
.venv/bin/python -m pytest -q                            # all tests
.venv/bin/python -m pytest -q tests/test_album_naming.py -k year   # single test
.venv/bin/ruff check gp2sm tests                         # lint (config in pyproject.toml)
scripts/test-pythons.sh                                  # tests on every local Python 3.11/3.12/3.14 (venvs in .venvs/); the only macOS testing (CI has none): run before pushing or tagging
GP2SM_LIVE_SMUGMUG=smugmug_config.json .venv/bin/python -m pytest -q -m live   # destination contract vs real SmugMug (sandbox folder, cleaned up)
.venv/bin/gp2sm --help                                   # all commands (gp2sm <command> --help for each)
```

Every command works on a project (a folder with `gp2sm.toml`; `gp2sm init` creates one). `gp2sm plan | apply [--yes] | verify | report | undo` run the project's pipeline; each pipeline also has its own steps:
- Takeout import: `gp2sm takeout index → inventory → dedupe → [review, review --read] → plan → stage → upload --yes → verify`, plus `report` and `remove IDS`.
- Organize: `gp2sm organize inventory → plan → report → apply [--yes] → verify`, plus `reconcile`, `undo <album>` and the gated `delete-*` steps.
- Album names: `gp2sm albums inventory [--sample N] → plan → report → [approve --name GLOB] → apply [--yes] → verify`, plus `undo`.
- Album settings: `[[policy]]` entries in gp2sm.toml, then `gp2sm albums audit [--sample N] → report → fix [--yes] → verify`, plus `undo`.
Re-running any step only adds new work. Backwards compatibility isn't a goal before the first public release: no shims, no legacy commands.

## Architecture (`gp2sm/`)

- `project/`: `config.py` (gp2sm.toml schema + validation), `credentials.py` (per-user credential store), `init.py`, and `context.py`. Every command's `main()` calls `context.add_args` + `context.resolve(args)`, which finds the project (`--project` or the nearest gp2sm.toml upward) and fills unset path args. It calls `context.client(cfg)` for the destination and `context.acquire_lock(state_db, label)` before destination-changing commands (stale locks are taken over).
- `services/`: `registry.py` discovers services via the `gp2sm.services` entry-point group (built-ins SmugMug and Google Takeout register the same way; plugins can't shadow built-ins; factories are checked against the protocol). `base.py` has the service-neutral `PhotoDestination`/`PhotoSource` protocols, records (`ItemRecord`, `AlbumRecord`, `SourceItem`) and `Capabilities`. Any destination must pass `tests/contracts/destination.py` (against the fake in CI; opt-in live run against SmugMug). Any source must pass `tests/contracts/source.py`.
- `smugmug/client.py`: the SmugMug adapter, and the only place that talks to SmugMug. It handles retries (network, 429/5xx, 401 `nonce_used`), `stat:"fail"` arriving with HTTP 200, paging, per-endpoint list keys, rate-limit headers, and ambiguous writes (never blindly retried). It declares `SMUGMUG_CAPABILITIES`.
- `takeout/`: `archive.py` (the only code that opens archives: `.zip` and `.tgz`, `iter_members`/`read_members`), `index.py` (one streaming pass: MD5, sidecars, and each media file's dimensions/duration/own capture time via `media.probe`), `items.py` (sidecar ↔ media pairing incl. `(N)` names, Live Photo clips), `source.py` (`TakeoutSource`, the PhotoSource), `cli.py` (`gp2sm takeout …`), `upload.py` (stage/upload/verify/remove rows of the `uploads` table; HEIC→JPEG keeping EXIF, filling in missing dates).
- `importer/` (service-neutral; any PhotoSource → any PhotoDestination): `inventory.py` snapshots the destination albums in scope (`list_folder_albums`, by folder display names) into `dest_albums`/`dest_items`; `dedupe.py` decides per source item `exact | same | new | review | source_duplicate` (pure `decide`, cached dHashes in `hash_source`/`hash_dest`, results in `source_matches`; a person's `reviewed` answer wins). Wired to Takeout by `takeout/cli.py` (`gp2sm takeout …`).
- `organize/`:
  - `rules.py`: pure organize rules: the date chain (`camera` → `filename` patterns → `upload`, plus plugins), `[[organize.group]]` grouping (first match wins, else `unassigned`), album names with `{yyyy}`/`{mm}`/`{group}`, `skip_newer_than_days`
  - `planning.py`: pure organize planning (`plan_organize`, soft-cap parts)
  - `cli.py`: `gp2sm organize`: inventory by names, rules → `planning.plan_organize` → `engine.write_plan`
  - `engine.py`: inventory storage, plan writing, and the steps that change the destination (apply with move or collect, reconcile, verify, undo, gated deletions; `ensure_target` is also used by the Takeout uploader)
- `state/`: the SQLite schema with service-neutral names (`items`, `item_id`/`item_ref`, `album_id`/`album_ref`, `targets`, `plan`, `uploads`, importer and album-management tables), plus `migrations.py` (version 1 is the neutral schema; later steps are appended with a test upgrading the previous version; pre-release databases are refused with `PreReleaseState`, newer ones with a "upgrade gp2sm" error). `plan.status` goes pending → in_progress → done | failed, with `unknown` meaning "ask the server"; parked duplicates record `keeper_item_id`, which `delete-duplicates` checks on the server. Every action is also appended to `events`.
- `media/`: `probe.py` (`probe(data, ext)`: dimensions, duration, own capture time as ISO with offset when recorded), `still.py` (EXIF via Pillow), `mp4.py` (MP4 header parsing: duration, dimensions, aspect, clip capture time: Apple `creationdate` else `mvhd`) and `convert.py` (cross-platform HEIC/any→JPEG via Pillow + pillow-heif; original EXIF bytes passed through untouched, Orientation reset to 1 since libheif applies the rotation; `render_small` for hashing; optional `sips` backend on macOS).
- `albums/`: `policy.py` (pure: which `[[policy]]` applies to an album, drift in a safe order, warnings such as folder-capped privacy, findings such as empty or nearly full albums), `naming.py` (pure: dates in album names with confidence, rename proposals from templates, dating name-less albums from tightly clustered photo samples) and `cli.py` (`gp2sm albums inventory|plan|approve|apply|audit|fix|report|verify|undo`; renames and setting changes recorded in `album_changes` with old values; display names only, never `UrlName`). Neutral setting names and values are in `services/base.py` (`ALBUM_SETTING_VALUES`); a destination declares the ones it supports in `Capabilities.album_settings`.
- `report/`: `model.py` (the report as plain data: per-file accounting where every source file gets exactly one outcome and the totals are checked, plus evidence, verification, activity), `render.py` (Markdown, HTML, JSON, CSV), `cli.py` (`gp2sm report`, written to the project's `reports/`).
- `cli/`: the `gp2sm <command>` dispatcher, `status`, `services`, `auth`, and `run.py`, which every tool uses: `add_yes` (anything that changes the destination is a dry run without `--yes`), `install_sigint`/`Stop` (first Ctrl-C finishes the work in flight; second aborts), `run_command` (records the run as ok/stopped/interrupted/failed, releases the project lock, exit 130 on abort) and `Progress`.

## Modularity rule

The goal is a plugin architecture with any photo service on either end (see `docs/ROADMAP.md` A1). Don't add to the existing coupling:
- Only `smugmug/client.py` may build SmugMug URLs, make HTTP calls to SmugMug, or know SmugMug field names and quirks. Everything else uses its **neutral surface** (`list_albums`, `list_album_items`, `item_ref`, `move_items`, `album_contains`, `item_album_ids`, `album_item_count`, `upload_file`, `remove_item`, `preview_bytes`, `root_folder`, `set_sort_by_filename`, `ensure_folder_path`, `ensure_album`, `delete_album`), which returns plain dicts (`item_id`, `item_ref`, `name`, `md5`, `size`, `width`, `height`, `is_video`, `duration_s`, `capture_time`, …). If logic needs something new, add a neutral method there.
- The state schema is service-neutral; keep it that way (service-specific values go in `raw`/`raw_metadata` JSON, read only by the adapter's own code).
- Only `takeout/` may know Google Takeout layout and metadata formats.
- New decision logic goes in pure functions that take neutral values (names, timestamps, hashes, dimensions), not service field names.

## Probes

`probes/` (gitignored) holds standalone scripts that exercise real APIs. Outputs, logs and copies of credentials go in `probes/out/`. Probes always work on **copies** of credential files. Note: the existing probes import the removed legacy SmugMug module for auth, so port them to `SmugMugClient` before reusing them. Write tests go only into a private `gp2sm-sandbox` folder, which the probe deletes afterwards.

## Gotchas

- Google: the Google Photos API can no longer export a library; Google Takeout is the only way out, and it always contains the whole service (no album selection).
- SmugMug collect: removing an item from its **original** album deletes every collected copy. Undo a collect by removing the copy in the target only; never delete or empty a source album items were collected from (organize refuses `delete-empty-sources` with collect).
- SmugMug album settings: read privacy from the album's **node** (`Privacy`, `EffectivePrivacy`); `Album.Privacy` shows the effective value. Several PATCHes return 200 and do nothing (`Date`; `MaxPhotoDownloadSize` while downloads are off; `UrlName` collisions with `AutoRename`). Changing `UrlName` breaks old links. See `docs/smugmug-api.md`.
- SmugMug converts HEIC to JPEG on upload (`NAME.HEIC` becomes `NAME.JPG`, and the original isn't kept). It re-encodes videos, rejects WebP/ICO/BMP and tiny videos (code 64 or 6), and silently accepts duplicate uploads.
- `AlbumImage.Date` is the upload time. Capture time comes from `ImageMetadata.DateTimeCreated` (use `_expand=ImageMetadata` on `!images`).
- Uploads converted by other tools often lost their EXIF: dates then come from file or album names, or the photos' neighbours (see `organize/rules.py`, `albums/naming.py`).

## Code style

- Don't change `__version__` or TODO comments unless asked.
- One statement per line. Never put a block body on the same line as `if`/`for`/`with`/`try`.
- Use `logging`, not `print`, except for CLI output and interactive prompts.
- Never wrap imports in try/except.
- Keep each API's code in its own module.
