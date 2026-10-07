# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## State of the repo

- The legacy v1/v2 script was removed. It's preserved at the git tag `legacy-v2`, and none of its code is trusted (core parts never worked). All work is in the `gp2sm/` package.
- `docs/ROADMAP.md` is the staged plan (do one stage at a time). `docs/smugmug-api.md` records SmugMug behavior **confirmed by probes**. Check it before relying on any SmugMug endpoint, and add anything newly confirmed.
- The repo is meant to become public. Keep personal names, account details and real album names out of tracked files. They belong in gitignored config (`data/`, `*.json`).

## Commands

```bash
.venv/bin/pip install -e '.[dev]'                        # install (editable) with pytest + ruff
.venv/bin/python -m pytest -q                            # all tests
.venv/bin/python -m pytest -q tests/test_planning.py -k heic   # single test
.venv/bin/ruff check gp2sm tests                         # lint (config in pyproject.toml)
GP2SM_LIVE_SMUGMUG=smugmug_config.json .venv/bin/python -m pytest -q -m live   # destination contract vs real SmugMug (sandbox folder, cleaned up)
.venv/bin/gp2sm --help                                   # all commands (gp2sm <command> --help for each)
```

Consolidation pipeline (`gp2sm consolidate <step>`; each step can be rerun; state lives in `data/consolidation.db`):
`inventory → import-legacy → match → plan → report → apply [--yes|--limit N|--target GLOB] → verify`, plus `reconcile` and `undo <album name>` for recovery.

Takeout import (A3, any project; archives in the project's `takeout/` folder):
`gp2sm takeout index → inventory → dedupe → [review, review --read] → plan → stage → upload --yes → verify`, plus `report`. Re-running any step only adds new work.

Legacy migration tools (the first real migration's state, `data/`): `takeout-match`, `content-match`, `takeout-upload`, `place-clips`, `date-undated`.

## Architecture (`gp2sm/`)

- `project/`: `config.py` (gp2sm.toml schema + validation), `credentials.py` (per-user credential store), `init.py`, and `context.py`. Every tool's `main()` calls `context.add_args` + `context.resolve(args)`, which picks `--config` JSON > `--project`/nearest gp2sm.toml > legacy `data/consolidate.json` and fills unset path args. It calls `context.client(cfg)` for the destination and `context.acquire_lock(state_db, label)` before destination-changing commands (stale locks are taken over).
- `services/`: `registry.py` discovers services via the `gp2sm.services` entry-point group (built-ins SmugMug and Google Takeout register the same way; plugins can't shadow built-ins; factories are checked against the protocol). `base.py` has the service-neutral `PhotoDestination`/`PhotoSource` protocols, records (`ItemRecord`, `AlbumRecord`, `SourceItem`) and `Capabilities`. Any destination must pass `tests/contracts/destination.py` (against the fake in CI; opt-in live run against SmugMug). Any source must pass `tests/contracts/source.py`.
- `smugmug/client.py`: the SmugMug adapter, and the only place that talks to SmugMug. It handles retries (network, 429/5xx, 401 `nonce_used`), `stat:"fail"` arriving with HTTP 200, paging, per-endpoint list keys, rate-limit headers, and ambiguous writes (never blindly retried). It declares `SMUGMUG_CAPABILITIES`.
- `takeout/`: `archive.py` (the only code that opens archives: `.zip` and `.tgz`, `iter_members`/`read_members`), `index.py` (one streaming pass: MD5, sidecars, and each media file's dimensions/duration/own capture time via `media.probe`), `items.py` (sidecar ↔ media pairing incl. `(N)` names, Live Photo clips), `source.py` (`TakeoutSource`, the PhotoSource), `cli.py` (`gp2sm takeout …`), `upload.py` (stage/upload/verify/remove rows of the `uploads` table; HEIC→JPEG keeping EXIF, filling in missing dates).
- `importer/` (service-neutral; any PhotoSource → any PhotoDestination): `inventory.py` snapshots the destination albums in scope (`list_folder_albums`, by folder display names) into `dest_albums`/`dest_items`; `dedupe.py` decides per source item `exact | same | new | review | source_duplicate` (pure `decide`, cached dHashes in `hash_source`/`hash_dest`, results in `source_matches`; a person's `reviewed` answer wins). Wired to Takeout by `takeout/cli.py` (`gp2sm takeout …`).
- `organize/`:
  - `rules.py`: pure organize rules: the date chain (`camera` → `filename` patterns → `upload`, plus plugins), `[[organize.group]]` grouping (first match wins, else `unassigned`), album names with `{yyyy}`/`{mm}`/`{group}`, `skip_newer_than_days`
  - `planning.py`: pure matching, duplicate grouping and album planning
  - `consolidate.py`: inventory → match → plan → apply → verify, reconcile/undo, gated deletions
  - `place_clips.py`: unsorted clips → beside their still, by time + aspect
  - `date_undated.py`: evidence chain for undated items, server-confirmed moves
- `state/`: the SQLite schema, plus `migrations.py` (ordered, versioned, idempotent steps recorded in `schema_history`; new DBs are created at LATEST; to add one, append a step, update SCHEMA, and test an upgrade from the previous version). `plan.status` goes pending → in_progress → done | failed, with `unknown` meaning "ask the server". Every action is also appended to `events`. Specific TODOs for a neutral, versioned schema are in `state/__init__.py`.
- `media/`: `probe.py` (`probe(data, ext)`: dimensions, duration, own capture time as ISO with offset when recorded), `still.py` (EXIF via Pillow), `mp4.py` (MP4 header parsing: duration, dimensions, aspect, clip capture time: Apple `creationdate` else `mvhd`) and `convert.py` (cross-platform HEIC/any→JPEG via Pillow + pillow-heif; original EXIF bytes passed through untouched, Orientation reset to 1 since libheif applies the rotation; `render_small` for hashing; optional `sips` backend on macOS).
- `contrib/legacy_bridge/`: only for libraries first moved with the old v1/v2 tool: `takeout_match` (link a Takeout to the legacy transfer DB), `content_match` (dHash vs legacy uploads, bursts), `takeout_upload` (that migration's upload plan). `consolidate`'s legacy import moves here in A4.
- `cli/`: the `gp2sm <command>` dispatcher, `status`, `services`, `auth`, and `run.py`, which every tool uses: `add_yes` (anything that changes the destination is a dry run without `--yes`), `install_sigint`/`Stop` (first Ctrl-C finishes the work in flight; second aborts), `run_command` (records the run as ok/stopped/interrupted/failed, releases the project lock, exit 130 on abort) and `Progress`.

## Modularity rule (until Stage A1 adds the service layer)

The goal is a plugin architecture with any photo service on either end (see `docs/ROADMAP.md` A1). Don't add to the existing coupling:
- Only `smugmug/client.py` may build SmugMug URLs, make HTTP calls to SmugMug, or know SmugMug field names and quirks. Everything else uses its **neutral surface** (`list_albums`, `list_album_items`, `item_ref`, `move_items`, `album_contains`, `item_album_ids`, `album_item_count`, `upload_file`, `remove_item`, `preview_bytes`, `root_folder`, `set_sort_by_filename`, `ensure_folder_path`, `ensure_album`, `delete_album`), which returns plain dicts (`item_id`, `item_ref`, `name`, `md5`, `size`, `width`, `height`, `is_video`, `duration_s`, `capture_time`, …). If logic needs something new, add a neutral method there.
- The state schema is still SmugMug-/Google-shaped (see the TODO in `state/__init__.py`). Map columns to neutral names at the boundary (`SELECT image_key AS item_id`) rather than spreading service names into logic.
- Only `takeout/` may know Google Takeout layout and metadata formats.
- New decision logic goes in pure functions that take neutral values (names, timestamps, hashes, dimensions), not service field names.

## Probes

`probes/` (gitignored) holds standalone scripts that exercise real APIs. Outputs, logs and copies of credentials go in `probes/out/`. Probes always work on **copies** of credential files. Note: the existing probes import the removed legacy SmugMug module for auth, so port them to `SmugMugClient` before reusing them. Write tests go only into a private `gp2sm-sandbox` folder, which the probe deletes afterwards.

## Gotchas

- Google: the daily quotas are unpublished, and once you hit one you're blocked until the reset. Treat a 429 as "stop for the day", not something to retry.
- SmugMug converts HEIC to JPEG on upload (`NAME.HEIC` becomes `NAME.JPG`, and the original isn't kept). It re-encodes videos, rejects WebP/ICO/BMP and tiny videos (code 64 or 6), and silently accepts duplicate uploads.
- `AlbumImage.Date` is the upload time. Capture time comes from `ImageMetadata.DateTimeCreated` (use `_expand=ImageMetadata` on `!images`).
- Legacy conversion uploads lost their EXIF. Their capture dates come from the legacy transfer DBs (`media_items.creation_timestamp`), joined in `matches`.

## Code style

- Don't change `__version__` or TODO comments unless asked.
- One statement per line. Never put a block body on the same line as `if`/`for`/`with`/`try`.
- Use `logging`, not `print`, except for CLI output and interactive prompts.
- Never wrap imports in try/except.
- Keep each API's code in its own module.
