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
`inventory → import-legacy → match → plan → report → apply [--dry-run|--limit N|--target GLOB] → verify`, plus `reconcile` and `undo <album name>` for recovery.

Takeout pipeline (archives in `data/takeout/`, all gitignored):
`gp2sm takeout-index data/takeout/*.tgz` → `gp2sm takeout-match` → `gp2sm content-match [--apply]` → `gp2sm takeout-upload plan|stage|upload|verify|report|remove`, then `gp2sm place-clips plan|upload|verify|finalize` and `gp2sm date-undated plan|apply`.

## Architecture (`gp2sm/`)

- `services/`: service-neutral `PhotoDestination`/`PhotoSource` protocols, records (`ItemRecord`, `AlbumRecord`, `SourceItem`) and `Capabilities`. Any destination must pass `tests/contracts/destination.py` (against the fake in CI; opt-in live run against SmugMug). Any source must pass `tests/contracts/source.py`.
- `smugmug/client.py`: the SmugMug adapter, and the only place that talks to SmugMug. It handles retries (network, 429/5xx, 401 `nonce_used`), `stat:"fail"` arriving with HTTP 200, paging, per-endpoint list keys, rate-limit headers, and ambiguous writes (never blindly retried). It declares `SMUGMUG_CAPABILITIES`.
- `takeout/`: `index.py` (stream-index `.tgz`, MD5 + sidecars), `match.py` (sidecar ↔ media pairing incl. `(N)` names, Live Photo clips), `source.py` (`TakeoutSource`, the PhotoSource), `upload.py` (plan/stage/upload/verify Live Photo pairs and HEIC; HEIC→JPEG keeping EXIF, filling in missing dates; clip-time pairing guard).
- `organize/`:
  - `planning.py`: pure matching, duplicate grouping and album planning
  - `consolidate.py`: inventory → match → plan → apply → verify, reconcile/undo, gated deletions
  - `content_match.py`: dHash matching, burst pairing
  - `place_clips.py`: unsorted clips → beside their still, by time + aspect
  - `date_undated.py`: evidence chain for undated items, server-confirmed moves
- `state/`: the SQLite schema. `plan.status` goes pending → in_progress → done | failed, with `unknown` meaning "ask the server". Every action is also appended to `events`. Specific TODOs for a neutral, versioned schema are in `state/__init__.py`.
- `media/`: MP4 header parsing (duration, dimensions), aspect ratio.
- `cli/`: the `gp2sm <command>` dispatcher.

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
