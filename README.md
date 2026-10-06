# gp2sm

Tools for moving photo libraries into **SmugMug** and keeping them organized, with every step state-tracked, verified against the server, and undoable where possible.

> **Status: in active rework.** The modules below have been used for a real ~35,000-item migration (Google Takeout → SmugMug, with Live Photos and an earlier messy import consolidated). They're being turned into an installable, documented tool. See [`docs/ROADMAP.md`](docs/ROADMAP.md). The earlier v1/v2 script is preserved at the git tag `legacy-v2`.

## What it does today

- **Import from Google Takeout.** It reads the `.tgz` archives without extracting them, pairs metadata files and Live Photo motion clips, and uploads each Live Photo as a JPEG still plus its MP4 clip, side by side. HEIC is converted to JPEG with EXIF kept, and a missing capture date is filled in from the Takeout metadata.
- **Consolidate existing SmugMug albums.** It inventories them, links items to a source by hash, name + dimensions or image content (dHash), removes byte-identical duplicates, and moves everything into dated albums.
- **Sort what's left.** It places unsorted clips beside their stills by capture time + aspect ratio, and dates undated items from evidence (own timestamps, names, video duration and shape, content).
- **Safety model.** Writes are recorded before they're sent. Outcomes that can't be known (timeouts/5xx) are checked against the server instead of being retried blindly. Deletions need `--yes`. `verify` compares the server with the state database.

## Running it (current, pre-packaging)

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp consolidate.json.example data/consolidate.json   # then edit; data/ is gitignored
cp smugmug_config.json.example smugmug_config.json  # then fill in credentials

.venv/bin/gp2sm --help                 # lists all commands
.venv/bin/gp2sm consolidate --help
.venv/bin/python -m pytest -q
```

A single `gp2sm` command, project setup (`gp2sm init`) and interactive SmugMug sign-in are planned in the roadmap (stages A0–A2).

## Documentation

- [`docs/ROADMAP.md`](docs/ROADMAP.md): staged plan
- [`docs/smugmug-api.md`](docs/smugmug-api.md): SmugMug API v2 behavior confirmed by probes, including several undocumented traps

## License

Public domain ([The Unlicense](LICENSE)). Not affiliated with Google or SmugMug.
