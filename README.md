# gp2sm

Tools for moving photo libraries into **SmugMug** and keeping them organized, with every step state-tracked, verified against the server, and undoable where possible.

> **Status: in active rework.** The modules below have been used for a real ~35,000-item migration (Google Takeout → SmugMug, with Live Photos and an earlier messy import consolidated). They're being turned into an installable, documented tool. See [`docs/ROADMAP.md`](docs/ROADMAP.md). The earlier v1/v2 script is preserved at the git tag `legacy-v2`.

## What it does today

- **Import from Google Takeout** (`gp2sm takeout …`). It reads the `.zip` or `.tgz` archives without extracting them, and checks what's already on SmugMug: same bytes, the same picture re-encoded (perceptual hash), or the same video shape. Only genuinely unclear cases are held for you, as side-by-side files you drag into `same/` or `different/`. New items go into month albums. Live Photo clips are paired with their still by capture time and shape and uploaded beside it under the same name. HEIC is converted to JPEG with EXIF kept (or uploaded as is), and a missing capture date is filled in from the Takeout metadata. Each choice is a setting in `gp2sm.toml`.
- **Consolidate existing SmugMug albums.** It inventories them, links items to a source by hash, name + dimensions or image content (dHash), removes byte-identical duplicates, and moves everything into dated albums.
- **Sort what's left.** It places unsorted clips beside their stills by capture time + aspect ratio, and dates undated items from evidence (own timestamps, names, video duration and shape, content).
- **Safety model.** Writes are recorded before they're sent. Outcomes that can't be known (timeouts/5xx) are checked against the server instead of being retried blindly. Anything that changes SmugMug is a dry run unless you add `--yes`. Ctrl-C once stops after the work in flight; twice aborts, and the next run picks up where it left off. `verify` compares the server with the state database.

## Running it (current, pre-packaging)

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/gp2sm init my-library          # creates my-library/gp2sm.toml (commented) and folders
.venv/bin/gp2sm auth smugmug             # sign in once; credentials are stored per user, outside the project
cd my-library && ../.venv/bin/gp2sm status

.venv/bin/gp2sm plan                     # dry: what would happen (Takeout import and/or organize)
.venv/bin/gp2sm apply --yes              # do it; then: gp2sm verify, gp2sm report
.venv/bin/gp2sm --help                   # lists all commands
.venv/bin/python -m pytest -q
```

Commands find the project by looking upward from the current folder for `gp2sm.toml` (or use `--project DIR`). The state database, logs, Takeout archives and index all live in the project folder. Commands that change SmugMug take a per-project lock, so two runs can't collide. The older JSON setup (`--config FILE`, or `data/consolidate.json`) still works. [`examples/`](examples) has commented configs for common setups.

## Documentation

- [`docs/ROADMAP.md`](docs/ROADMAP.md): staged plan
- [`docs/smugmug-api.md`](docs/smugmug-api.md): SmugMug API v2 behavior confirmed by probes, including several undocumented traps

## License

Public domain ([The Unlicense](LICENSE)). Not affiliated with Google or SmugMug.
