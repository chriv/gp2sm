# SmugMug API v2: Verified Behavior

Only behavior **observed in a probe** goes here, with the date checked. SmugMug's official docs are incomplete and sometimes wrong. If this file disagrees with them, trust this file and re-verify. Keys and URIs below are placeholders.

Probe scripts live in `probes/` (gitignored). Raw request/response logs are written to `probes/out/`.

## Auth and rate limits (2026-10-05)
- OAuth 1.0a (`requests_oauthlib.OAuth1Session`), with an access token from the out-of-band (`oob`) PIN flow. Tokens didn't expire between 2025-04 and 2026-10.
- Every response includes `x-ratelimit-remaining` (about 100,000 observed) and `x-ratelimit-reset` (epoch seconds). Read them and slow down as the remaining count falls.
- **`401 {"Message":"oauth_problem=nonce_used"}`** happened on a 1,000-item page that took about 60 seconds. It looks like a server-side timeout followed by an internal retry with the same nonce. Treat it as retryable, and avoid slow requests (see paging).
- Use one `OAuth1Session` per thread.
- **A write that returns 504 may still have been applied.** (Observed on 2026-10-05: a 50-item `!moveimages` returned HTTP 504, but all 50 images had moved. Retrying the same move then returned 400, because the URIs no longer pointed into the source album.) Never blindly retry a POST/PATCH/DELETE after a timeout, 5xx or network error. Check the server's state instead (`GET /api/v2/image/{key}-{serial}!albums`). It's safe to retry 429 and `nonce_used`, because those requests were rejected.

## Response shapes
- All API responses: `{"Response": {...}, "Code": ..., "Message": ...}`. Failures may also carry `stat: "fail"`, `code`, `message`.
- **The response key depends on the endpoint:**

| Endpoint | List key |
|---|---|
| `GET /api/v2/user/{nick}!albums` | `Album` |
| `GET /api/v2/album/{key}!images` | **`AlbumImage`** (not `Image`) |
| `GET /api/v2/node/{id}!children` | `Node` |
| `GET {image}!metadata` | `ImageMetadata` |
| `GET {image}!largestvideo` | `LargestVideo` |
| `GET /api/v2/image!search` | `Image` (empty result → key missing) |

- POST `!moveimages` / `!collectimages` / DELETE responses: HTTP 200 with `"Message": "Ok"` and no `stat`.

## Paging and field selection
- Paging uses `count` + `start`. Follow `Response.Pages.NextPage`, a relative URI that already includes your params. `Pages.Total` gives the total count.
- `count`: 100 ≈ 2 s per page. 500–1,000 ≈ 18–22 s and risks `nonce_used`. 5,000 returned no `Response`. **Use 100–200.**
- **`_filter` is a FIELD selector, not a value filter.** `_filter=FileName,ArchivedMD5` limits which fields come back. `_filtervalue` is ignored, so it can't be used to search by MD5. `_filteruri=` (empty) drops the large `Uris` block.

- **`_expand=ImageMetadata` on `!images`** returns each item's metadata in one call. It's in the top-level `Expansions` map, keyed by the item's `Uris.ImageMetadata.Uri`. A 49-item page took 1.3 s. This avoids one `!metadata` call per image. (Don't combine it with `_filteruri=`, which removes the URIs used as keys.)

- **`_expand=ImageMetadata` can return stale, empty metadata for recently uploaded images.** (Observed on 2026-10-06: 18 of 27 new uploads still showed `DateTimeCreated` empty through `_expand` after more than 4 minutes, while a direct `GET {ImageUri}!metadata` returned the correct dates.) Use direct calls when verifying fresh uploads.
- `GET /api/v2/image/{key}-{serial}` also returns `DateTimeOriginal`, which isn't in the `!images` listing. SmugMug converts EXIF local time to UTC there **without** using `OffsetTimeOriginal` (a −04:00 photo came back +7 h), so treat it as display-only. `!metadata`'s `DateTimeCreated` keeps the original local time.
- Album `SortMethod` can be set with `PATCH /api/v2/album/{key}` (`{"SortMethod":"FileName","SortDirection":"Ascending"}`). It's echoed back as `"Filename"`.

## Search
- `GET /api/v2/image!search?Scope=<album uri>&Text=<exact filename>` returned **0 results for a file known to be in the album**. Don't use it for existence checks. Use a local inventory built from `!images` listings.

## Albums and folders
- Create: `POST {parentNodeUri}!children` with JSON `{"Type": "Folder"|"Album", "Name", "UrlName", "Privacy": "Private"}` → 201, `Response.Node`. The album URI is `Node.Uris.Album.Uri`.
- `UrlName` must be unique among siblings.
- `Album.ImageCount` updates immediately after upload, move, collect and delete.
- **Album capacity:** documented as 5,000. Albums holding **5,001** items exist on the account. Not yet tested at the limit. Upload failure code 63 means "album full" (from legacy code, not re-verified).
- Delete album: `DELETE /api/v2/album/{key}` → 200. Delete folder: `DELETE /api/v2/node/{id}` → 200.

## Upload
`POST https://upload.smugmug.com/` with the raw file bytes as the body and these headers:
`X-Smug-AlbumUri`, `X-Smug-FileName`, `X-Smug-ResponseType: JSON`, `X-Smug-Version: v2`, `Content-MD5` (hex), `Content-Type`.

- Success: HTTP 200, `{"stat": "ok", "Image": {"ImageUri", "AlbumImageUri", "StatusImageReplaceUri", "URL"}}`. Store `AlbumImageUri`/`ImageUri` right away.
- Failure: **still HTTP 200**, `{"stat": "fail", "code": N, "message": ...}`. Always check `stat`.
- **Identical duplicates are accepted silently**, even in the same album. Duplicate prevention is entirely the client's job.

| Type | Result (2026-10-05) |
|---|---|
| JPEG, PNG, GIF | Accepted. `ArchivedMD5` == MD5 of the uploaded bytes, `ArchivedSize` == byte size. **MD5 matching works.** |
| HEIC | Accepted but **converted to JPEG**. `FileName` becomes `NAME.JPG`, and `ArchivedMD5`/`ArchivedSize` are the JPEG's. The original isn't kept. |
| MP4, MOV (H.264 and HEVC/hvc1) | Accepted, `Format: "MP4"`. Re-encoded: `ArchivedMD5`/`ArchivedSize` don't match the original. `FileName` keeps the original name and extension. |
| Very small videos (≤ about 40 KB) | **Rejected: code 64 "unknown file type"**, whatever the duration, extension or Content-Type. A 1.5 s / 3.4 MB MOV was accepted, and a 5 s / 4.5 KB MOV was rejected. Real camera videos aren't affected, but test fixtures must be larger than ~60 KB. |
| Low-resolution videos (2026-10-06, real Live Photo clips) | **Rejected: code 64 "unknown file type" or code 72 "video too small"**, decided by frame size, not duration: 152×114, 156×234, 192×258, 254×118 and 234×462 HEVC clips were rejected, while full-resolution clips as short as 0.43 s were accepted. The exact threshold is unknown (somewhere around 250–460 px on the longer side). Treat codes 64/72 on video as permanent. |
| WebP, ICO | Rejected: code 64 "unknown file type". |
| BMP | Rejected: code 6 "wrong format (RAW uploads require the Source add-on)". |

## Dates and video details
- `AlbumImage.Date` is the **upload time**, not the capture time.
- Capture time is in `GET {ImageUri}!metadata`: `DateTimeCreated` (EXIF DateTimeOriginal for images, container `creation_time` for videos), with no timezone. It survives the HEIC→JPEG conversion.
- `GET {ImageUri}!largestvideo` → `Duration` (seconds, as a string), `Size`, `MD5`, `Width`, `Height` of the *re-encoded* rendition.

## Renaming images
- **`FileName` can't be changed.** (Verified 2026-10-06 for both JPEG and MOV.) `PATCH /api/v2/image/{key}-0` with `{"FileName": ...}` returns **HTTP 200 "Ok" but leaves `FileName` unchanged**: a silent no-op. `Title` *can* be changed this way.
- Filename sort uses `FileName`, so the only way to change an item's sort position by name is to re-upload it under the new name and remove the old copy.

## Reorganizing
- Move: `POST /api/v2/album/{dest}!moveimages` with JSON `{"MoveUris": "<AlbumImage uri>[,<uri>...]"}` removes the image from the source album.
- Collect: `POST /api/v2/album/{dest}!collectimages` with JSON `{"CollectUris": "<AlbumImage uri>"}` keeps it in the source too, and counts toward the destination's `ImageCount`.
- Delete from an album: `DELETE <AlbumImage uri>` → 200.
- **Moving an item that's still processing is silently ignored.** (Observed 2026-10-06.) A video stuck in `Status: "Preprocess"`, `Processing: true` hours after upload: `!moveimages` returned `200 Ok`, but the item stayed in its source album, repeatably. **Always confirm a move on the server** (`image!albums`, or the target's `ImageCount` delta) instead of trusting the response, and retry once processing has finished.
- Moves occasionally fail with HTTP 500 without being applied. Once the server confirms the item didn't move, retrying is safe. (6 of 6 succeeded on retry.)

## Matching strategy implied by the above
- JPEG/PNG/GIF: MD5 of the source file == `ArchivedMD5`.
- HEIC: can't match by hash or by original filename. Use `{basename}.JPG` + `DateTimeCreated`, or record the `ImageKey` at upload time.
- Video: filename + `DateTimeCreated` + `Duration`.
- In every case, **recording `AlbumImageUri` at upload time** is the only exact link between a source item and its SmugMug copy.
