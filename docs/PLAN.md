# gp2sm v3 Plan (tentative)

Status: draft, written after the Stage A probes. Nothing here is built yet.

## Goal

Move one Google Photos library (~25k items: mostly HEIF/JPEG/PNG plus about 1.3k MP4) to SmugMug. Finish with a verified manifest that maps every source item to a SmugMug `ImageKey`, so the Google copy can be deleted by hand without risk.

## What the probes showed

| Area | Result |
|---|---|
| Google Photos Library API | **Dead for this use.** Even with `photoslibrary.readonly` granted, `albums.list` and `mediaItems.list` return 403 "insufficient authentication scopes" (March 2025 policy). |
| SmugMug auth / listing | Works. `!albums` paging works. Rate-limit headers are present (`x-ratelimit-remaining`, `x-ratelimit-reset`). |
| Legacy duplicate check | **Never worked.** Album listings return `AlbumImage`, not `Image`. `_filter` selects fields and doesn't filter by value (`_filtervalue` is ignored). `image!search` didn't find an exact filename. |
| Legacy album rotation | Created 7 albums within seconds, and its local counters disagreed with server counts. |
| Album cap | Two existing albums hold 5,001 items, so the 5,000 limit isn't exact. Use the server's count and a conservative threshold. |
| Legacy code overall | Not reused as-is. Individual pieces (OAuth1 setup, folder `!children` walking) may be adapted after testing. |

## Architecture

The pipeline runs as separate stages. Each stage:
- can be run again safely, giving the same result,
- stores its state in SQLite,
- keeps an append-only event log,
- supports `--dry-run`,
- can be run and resumed independently.

```
Source adapter ──► 1. Ingest ──► manifest (source items)
SmugMug account ─► 2. Inventory ──► local index of everything already on SmugMug
                   3. Reconcile ──► present / missing / ambiguous (human review)
                   4. Place ──► target album per item (allocator, cap-aware)
                   5. Upload ──► per-item state machine, records ImageKey, verifies after upload
                   6. Consolidate ──► move/collect earlier uploads into place, list extra copies for review
                   7. Verify & report ──► final source→ImageKey manifest ("safe to delete" list)
```

### Source adapters (from preferred to last resort)
1. **Takeout** (primary). Reads Takeout archives or extracted folders:
   - pairs media with `*.json` metadata files, including Google's truncated or `(1)`-suffixed sidecar names,
   - pairs Live Photo stills with their motion `.MOV`/`.MP4`,
   - collapses the same item appearing in several album folders, using a content hash,
   - doesn't touch the network or any Google quota.
2. **Picker API.** Has a real OAuth scope (`photospicker.mediaitems.readonly`) and works in user-selected sessions. Can be driven by Playwright if picking by hand is impractical. Google's daily quota applies.
3. **Browser-session scraping.** Playwright plus the authenticated web app's internal endpoints. Last resort: brittle, and it carries a risk of the account being flagged.

All three produce the same `SourceItem` record:
- source id
- local path or fetcher
- filename
- MIME type
- capture time
- size
- MD5 and SHA-256 of the original
- Live-pair link
- raw metadata

### SmugMug client (new; wrapper + docs required)
- One request function. It handles:
  - OAuth1,
  - `stat: fail` responses,
  - HTTP 429/5xx with backoff,
  - rate-limit headers,
  - paging via `Pages.NextPage`.
- Typed helpers that know which response key each endpoint uses (`AlbumImage` vs `Image` vs `Node`, etc.).
- `docs/smugmug-api.md` records **only behavior confirmed by a probe**, with the date it was checked and a sample response (with personal data removed).

### Album placement
- Target albums are grouped by capture date (e.g. `<Root>/<YYYY>/<YYYY-MM>`). Most albums then stay far under the cap, so rotation rarely happens.
- When a group would go over the threshold (default 4,000, using the **server's** `ImageCount` fetched just before writing), it splits into `… - Part N`.
- An album registry table records every album the tool creates. Each album gets one create attempt, logged with an idempotency key, so a crash can't produce a burst of albums.

### Matching rules (to be confirmed in Stage B)
- **Images:** local MD5 compared with SmugMug's `ArchivedMD5`.
- **Videos:** if `ArchivedMD5` turns out to be the hash of the *original* upload, compare MD5. Otherwise compare filename, capture time and duration, and send unclear cases to human review.
- **Converted files** (if any are converted): the record keeps both the source hash and the converted file's hash.

### Google call ledger
Every Google call is recorded with a timestamp in a ledger that lasts across runs. A 429 means "stopped for the day": record it, stop, and resume after the reset. No retries.

## Recovery and cleanup
- An item moves through `discovered → matched | queued → uploading → uploaded → verified`. If a run is interrupted mid-upload, the item is checked against SmugMug before it's uploaded again, so nothing is uploaded twice.
- Separate commands to:
  - list and delete albums or images created by a given run (with confirmation),
  - rebuild the local SmugMug index from the server,
  - export a state report.
- Data and config live outside the repo by default (e.g. `~/.local/share/gp2sm/`), or in gitignored paths.

## Testing
- **Unit (pytest):**
  - Takeout parsing against fake Takeout folders generated by the tests (no real media),
  - SmugMug client against recorded responses with personal data removed,
  - placement and cap logic,
  - state machine transitions.
- **Integration (marked, opt-in):** runs against a dedicated SmugMug sandbox folder and deletes what it creates.
- **Real runs in small batches:** 10 → 100 → 1,000 items before the full run. Each batch ends with a verification report.

## Milestones
| # | Milestone | Needs Takeout? |
|---|---|---|
| M0 | Repo reset: package layout, move legacy code to `legacy/`, pytest scaffold, config/data paths, gitignore | no |
| M1 | Stage B probes: upload JPEG/HEIC/PNG/MP4/MOV/WebP/ICO to a sandbox album, record upload responses, check `ArchivedMD5`/`ArchivedSize` against local files, test image move/collect/delete and album delete | no |
| M2 | SmugMug client + `docs/smugmug-api.md` + tests, built from M1 results | no |
| M3 | SmugMug inventory + duplicate report for existing conversion albums (user helps identify) | no |
| M4 | Takeout ingest adapter + tests (small real sample first) | yes |
| M5 | Reconcile + placement + uploader; real batches of 10/100/1,000 | yes |
| M6 | Consolidate earlier uploads; review and remove duplicates | yes |
| M7 | Full run, verification report, "safe to delete from Google" manifest | yes |

M0 through M3 can go ahead while the Takeout export is being prepared.

## Open decisions (owner: user)
1. SmugMug target structure: root folder name, grouping by year/month or something else.
2. Live Photo motion files: upload as separate short videos, store elsewhere, or drop.
3. Types SmugMug rejects (ICO, possibly WebP/HEIC; M1 will show which): convert, keep elsewhere, or skip.
4. Earlier uploads: move or collect them into the new structure, or treat them as disposable and re-upload.
5. Git author email in existing history before the repo goes public.
