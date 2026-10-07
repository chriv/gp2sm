# gp2sm

Move a photo library into **SmugMug** and keep it organized. Every step is planned before it runs, recorded, checked against SmugMug afterwards, and undoable where SmugMug allows it.

> **Status: beta (3.0.0b1).** Used on a real 35,000-item migration (Google Photos → SmugMug, Live Photos included). Expect rough edges; please report them.

## What it does

- **Import a Google Takeout** (`gp2sm takeout …`). It reads the Takeout `.zip` or `.tgz` archives without extracting them. Before uploading anything, it checks what's already on SmugMug: the same file, the same picture saved differently (compared by appearance), or the same video. Only genuinely unclear cases are held back for you, as side-by-side files you drag into `same/` or `different/`. Everything new goes into month albums. Live Photos arrive as a JPEG with its video clip beside it under the same name. HEIC photos are converted to JPEG with their camera data kept, and missing capture dates are filled in from the Takeout.
- **Organize what's already on SmugMug** (`gp2sm organize …`). It gathers items from the albums you name (for example a phone's auto-upload album) into month albums. Each item is dated by the camera, the file name, or the album name, and grouped by rules you write (by person, device, file name). Items are moved, or "collected" so the originals stay where the uploader app put them. Identical copies are parked for review.
- **Tidy album names and settings** (`gp2sm albums …`). It proposes date-sortable names like `2019-06 Beach Trip`, reading the date from the name or from the photos inside, and renames only the display name, so links keep working. It also checks every album against the settings you want (privacy, downloads, sort order, …) and fixes the differences.
- **Report** (`gp2sm report`). It accounts for what happened to every file: already there, uploaded, held for review, skipped and why. The counts have to add up, and the report says so.

## Install

```bash
pipx install gp2sm            # or: python -m pip install gp2sm   (Python 3.10 or newer)
gp2sm --help
```

From a checkout: `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`.

## Quickstart: Google Photos to SmugMug in about 10 minutes of your time

1. **Get a SmugMug API key** (once): see [docs/smugmug-api-key.md](docs/smugmug-api-key.md).
2. **Create a project** (a folder holding settings, progress and logs) and sign in:
   ```bash
   gp2sm init family-photos          # asks a few questions; writes family-photos/gp2sm.toml
   gp2sm auth smugmug                # API key and secret, then a 6-digit code from SmugMug
   cd family-photos
   ```
3. **Request a Google Takeout** of Google Photos and put the downloaded archives in `family-photos/takeout/`: see [docs/google-takeout.md](docs/google-takeout.md).
4. **Plan.** Nothing on SmugMug changes:
   ```bash
   gp2sm plan                        # indexes the archives, checks SmugMug, plans every upload
   gp2sm report                      # reports/report.html: what would happen to every file
   ```
   If the plan holds anything for review, run `gp2sm takeout review`, sort the pairs in `review/`, then `gp2sm takeout review --read` and `gp2sm plan` again.
5. **Upload and check:**
   ```bash
   gp2sm apply                       # prepares the files locally, then shows what it would upload
   gp2sm apply --yes                 # uploads (stop any time with Ctrl-C; run it again to continue)
   gp2sm verify                      # confirms every upload on SmugMug
   gp2sm report
   ```

Every setting (album names, HEIC handling, Live Photo clips, what to skip) is in `gp2sm.toml`, with its help text. See [docs/config.md](docs/config.md).

## Safety

Anything that changes SmugMug is a dry run unless you add `--yes`. Deletions are separate commands that check the server first. Every change is recorded before it's sent. Outcomes that can't be known (a timeout, a server error) are checked against SmugMug before anything is retried. One command at a time can change a project. See [docs/safety.md](docs/safety.md) for what each command can undo.

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
