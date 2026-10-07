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
- Paging uses `count` + `start`. `Response.Pages.NextPage` is a relative URI carrying only `count` and `start`: **it drops `_expand` (and other `_` params)**, so page 2 onwards silently comes back without expansions (confirmed 2026-10-07). Take the position from `NextPage` and resend your own params. `Pages.Total` gives the total count.
- `count`: 100 ≈ 2 s per page. 500–1,000 ≈ 18–22 s and risks `nonce_used`. 5,000 returned no `Response`. **Use 100–200.**
- **`_filter` is a FIELD selector, not a value filter.** `_filter=FileName,ArchivedMD5` limits which fields come back. `_filtervalue` is ignored, so it can't be used to search by MD5. `_filteruri=` (empty) drops the large `Uris` block.

- **`_expand=ImageMetadata` on `!images`** returns each item's metadata in one call. It's in the top-level `Expansions` map, keyed by the item's `Uris.ImageMetadata.Uri`. A 49-item page took 1.3 s. This avoids one `!metadata` call per image. (Don't combine it with `_filteruri=`, which removes the URIs used as keys.)

- **`_expand=ImageMetadata` can return stale, empty metadata for recently uploaded images.** (Observed on 2026-10-06: 18 of 27 new uploads still showed `DateTimeCreated` empty through `_expand` after more than 4 minutes, while a direct `GET {ImageUri}!metadata` returned the correct dates.) Use direct calls when verifying fresh uploads.
- `GET /api/v2/image/{key}-{serial}` also returns `DateTimeOriginal`, which isn't in the `!images` listing. SmugMug converts EXIF local time to UTC there **without** using `OffsetTimeOriginal` (a −04:00 photo came back +7 h), so treat it as display-only. `!metadata`'s `DateTimeCreated` keeps the original local time.
- **`!moveimages` sometimes fails with HTTP 500 or 504** (several times on 2026-10-07: a 504 in a 7,400-item run, 500s in the live contract moving seconds-old uploads). Later runs succeeded. Treat it as ambiguous: check where the items are before retrying (organize does).
- **An album's `!images` listing lags behind deletions.** Minutes after `DELETE` succeeded, the listing still showed some deleted items (31 of ~440 shortly after, 3 a few minutes later) while `GET /image/{key}` returned 404. Treat a 404 on a listed item as already gone, and don't use the listing alone to confirm a delete. (Confirmed 2026-10-07.)
- **Item keywords:** `!images` returns `KeywordArray` (and `Keywords`, a `;`-joined string) on each AlbumImage. SmugMug **adds keywords automatically from the file name**, split on separators (e.g. a UUID-named file gets its hex fragments as keywords), so a person's own tag (like `keep`, added in the web UI) sits among generated ones. Match whole keywords exactly, ignoring case. (Confirmed 2026-10-07.)
- Album `SortMethod` can be set with `PATCH /api/v2/album/{key}` (`{"SortMethod":"FileName","SortDirection":"Ascending"}`). It's echoed back as `"Filename"`.

## Search
- `GET /api/v2/image!search?Scope=<album uri>&Text=<exact filename>` returned **0 results for a file known to be in the album**. Don't use it for existence checks. Use a local inventory built from `!images` listings.

## Albums and folders
- Create: `POST {parentNodeUri}!children` with JSON `{"Type": "Folder"|"Album", "Name", "Privacy": "Private"}` (`UrlName` optional) → 201, `Response.Node`. The album URI is `Node.Uris.Album.Uri`.
- `UrlName` must be unique among siblings.
- `Album.ImageCount` updates immediately after upload, move, collect and delete.
- **Album capacity:** documented as 5,000. Albums holding **5,001** items exist on the account. Not yet tested at the limit. Upload failure code 63 means "album full" (from legacy code, not re-verified).
- Delete album: `DELETE /api/v2/album/{key}` → 200. Delete folder: `DELETE /api/v2/node/{id}` → 200.
- **Albums under a folder, by display names:** walk `GET {nodeUri}!children` from the root node (`!authuser` → `Uris.Node`), matching `Type: "Folder"` and `Name` for each path part, then recurse. Children of `Type: "Album"` carry the album key in `Uris.Album.Uri`. `Album.UrlPath` is URL-ified (spaces become dashes), so don't match display folder names against it. (Confirmed 2026-10-06 by the live destination contract, `list_folder_albums`.)

## The SmugMug apps' automatic upload (observed by the owner, 2026-10-07)

**Android** is different from iOS: you choose the album it uploads to, it makes **no** month sub-galleries, and when the album fills up it continues in a new, numbered album. These are the albums that need organizing into months. Not yet tested on Android: whether moving or deleting uploaded items makes the app upload them again, so use `mode = "collect"` (originals stay put) until that's known.

**iOS:**
- Uploads go to a root folder **"Automatic iOS Uploads"**, which the app creates and names (you can't choose). Inside it the app makes one gallery per month, named `YYYY-MM`, under a year folder. Older accounts can also have an earlier "Automatic iOS Uploads" folder elsewhere (e.g. under another folder) that the app no longer writes to.
- **Live Photos lose their motion**: only the still is uploaded.
- Deleting an uploaded photo on SmugMug does **not** make the app upload it again. Editing a photo on the device after it was uploaded does **not** upload a new copy. Deleting a photo on the device does **not** delete it on SmugMug (SmugMug is a backup, not a mirror).
- Galleries the iOS app creates carry search settings outside the documented values: `SmugSearchable` empty and `WorldSearchable` `3` (seen on every app-created gallery in a 30-album sample, 2026-10-07). gp2sm reports those for review instead of changing them, since the original value couldn't be written back.
- So the app's own month galleries rarely need organizing. The cleanup that matters is merging an old upload folder's galleries into the current one, and removing duplicates.

## Album and folder settings (probed 2026-10-07 in a sandbox: probes/probe_settings_*.py)

`OPTIONS` on an album or folder node lists its PATCH parameters with types and allowed values (50 for albums, 19 for folders), but the list isn't the whole truth: some listed values are refused, and some changes "succeed" without happening. Always read a value back after a PATCH.

**Album settings that round-trip** (set, read back, restored): `SmugSearchable` (`No` / `Inherit from User`), `WorldSearchable` (bool), `AllowDownloads`, `LargestSize` (Medium … X5Large, 4K, 5K, Original), `Protected`, `Watermark`, `Share`, `Comments`, `CanRank`, `EXIF`, `Filenames`, `Geography`, `Slideshow`, `Printable`, `HideOwner`, `SortMethod` (Position, Caption, Filename, Date Uploaded, Date Modified, Date Taken), `SortDirection`, `Description`, `Keywords`, `Title`.

**Traps:**
- **Privacy:** set it on the album, but read it from the album's **node**. `Node.Privacy` is the album's own setting and `Node.EffectivePrivacy` is what applies. `Album.Privacy` reports the *effective* value: an album in a Private folder reads "Private" whatever it is set to, and setting it to Public there returns 200 while appearing unchanged. A Private folder makes everything in it effectively Private. An Unlisted folder does **not** cap a Public album (it stays effectively Public).
- **`MaxPhotoDownloadSize`** is silently ignored while `AllowDownloads` is off, and **turning downloads off resets it to `Original`** (it isn't remembered when downloads come back on; confirmed 2026-10-07). With downloads on it round-trips, independent of `LargestSize`. A policy should only check the download size together with downloads on.
- **Album `Date`** isn't a PATCH parameter. Sending it returns 200 and changes nothing.
- **Folder `SmugSearchable` `Local`/`LocalUser`/`Yes` and `WorldSearchable` `HomeOnly`/`Yes`** are listed but refused with 400, even on a Public folder. Only `No` and `Inherit from User` were accepted (account-level settings may be what decides).

**Names and links:**
- Changing an album's **`Name`** leaves its `UrlName` and links unchanged.
- Changing **`UrlName`** moves the album's URL, and the **old path stops resolving** (`!urlpathlookup` on it returns no album; nothing redirects). Renaming `UrlName` breaks existing links, so only do it when the owner asks.
- A `UrlName` that a sibling already uses is refused with **HTTP 409**. With `AutoRename: true` the same PATCH returns 200 and changes nothing (AutoRename doesn't help on PATCH).
- Two albums in one folder may share the same display `Name`.
- A `UrlName` you **supply** must start with a letter ("2019-06-14 Beach Trip" had to be sent as `A-2019-06-14-Beach-Trip`). **Leave `UrlName` out** when creating and SmugMug derives it from the name, digits first allowed: `2016` → `2016`, `2016-08` → `2016-08`, `Beach Trip` → `Beach-Trip` (confirmed 2026-10-07; gp2sm no longer sends one). The create response's `Node` already carries the assigned `UrlName` and `UrlPath`. Renaming the display name later doesn't change it.
- Paging: `!images?start=N&count=M` (1-based `start`) fetches any page directly, which is how large albums are sampled.

**Folder settings that round-trip:** `Privacy` (Public, Unlisted, Private), `SortMethod` (SortIndex, Name, DateAdded, DateModified), `SortDirection`, `Description`, `ShowCoverImage`.

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
- `!largestvideo` returns **404 for some videos** (2026-10-07: 7 of a few hundred in a real library; likely still processing or without a playable rendition). Treat it as "duration unknown", never as "absent".
- **Listing cost:** about 25–35 ms of server time per item in `!images`, whatever the page size (100/500/1000 tried) or `_filter` field list (which saves only ~20%). A 35,000-item folder lists in about 3½ minutes with 6 albums in parallel. Durations need `_expand=ImageMetadata` or `!largestvideo`, so fetch them only for the videos that need them.

## Renaming images
- **`FileName` can't be changed.** (Verified 2026-10-06 for both JPEG and MOV.) `PATCH /api/v2/image/{key}-0` with `{"FileName": ...}` returns **HTTP 200 "Ok" but leaves `FileName` unchanged**: a silent no-op. `Title` *can* be changed this way.
- Filename sort uses `FileName`, so the only way to change an item's sort position by name is to re-upload it under the new name and remove the old copy.

## Reorganizing
- Move: `POST /api/v2/album/{dest}!moveimages` with JSON `{"MoveUris": "<AlbumImage uri>[,<uri>...]"}` removes the image from the source album.
- Collect: `POST /api/v2/album/{dest}!collectimages` with JSON `{"CollectUris": "<AlbumImage uri>"}` keeps it in the source too, and counts toward the destination's `ImageCount`.
- **Collect semantics** (probed 2026-10-07 in a sandbox, `probes/probe_collect.py`):
  - The collected copy is the same image (same `ImageKey`). `!albums` lists both albums, and its AlbumImage URI in the destination is `/album/{dest}/image/{key}-0`.
  - **Removing the collected copy** (`DELETE` its AlbumImage in the destination) leaves the original untouched. This is how to undo a collect.
  - **⚠️ Removing the original** from its own album **deletes the image everywhere**: the collected copies vanish too. Never delete or empty an album whose items were collected elsewhere.
  - Moving the original to another album keeps the collected copy, and the image is then in both of those albums.
  - Collecting an item that is already in the destination is a silent no-op (no error, no extra copy).
- Delete from an album: `DELETE <AlbumImage uri>` → 200.
- **Moving an item that's still processing is silently ignored.** (Observed 2026-10-06.) A video stuck in `Status: "Preprocess"`, `Processing: true` hours after upload: `!moveimages` returned `200 Ok`, but the item stayed in its source album, repeatably. **Always confirm a move on the server** (`image!albums`, or the target's `ImageCount` delta) instead of trusting the response, and retry once processing has finished.
- Moves occasionally fail with HTTP 500 without being applied. Once the server confirms the item didn't move, retrying is safe. (6 of 6 succeeded on retry.)

## Matching strategy implied by the above
- JPEG/PNG/GIF: MD5 of the source file == `ArchivedMD5`.
- HEIC: can't match by hash or by original filename. Use `{basename}.JPG` + `DateTimeCreated`, or record the `ImageKey` at upload time.
- Video: filename + `DateTimeCreated` + `Duration`.
- In every case, **recording `AlbumImageUri` at upload time** is the only exact link between a source item and its SmugMug copy.
