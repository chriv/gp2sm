# Configuration reference (`gp2sm.toml`)

Every project has a `gp2sm.toml`; `gp2sm init` writes one with every key and its help text.
Paths are relative to the project folder. Unknown keys and invalid values are reported all at once.
This page is generated from the code (`python -m gp2sm.project.reference`).

## `[project]`: General

| key | type | default | meaning |
|---|---|---|---|
| `name` | text | `""` | a name for this project (shown in reports) |
| `timezone` | time zone name | `"UTC"` | time zone for capture dates and month albums, e.g. America/New_York |

## `[destination]`: Where items go

| key | type | default | meaning |
|---|---|---|---|
| `service` | text | `"smugmug"` | destination service (see `gp2sm services`) |
| `credentials` | text | `"smugmug"` | name of the stored credentials (see `gp2sm auth`) |
| `folder` | text | `"Consolidated"` | folder that holds this project's albums |

## `[albums]`: Album names and limits

| key | type | default | meaning |
|---|---|---|---|
| `photo` | album name template | `"Photos {yyyy}-{mm}"` | dated album name; {yyyy} and {mm} come from the capture date, {group} from [[organize.group]] |
| `video` | album name template | `"Photos {yyyy}-{mm}"` | dated album for videos (same as photo keeps them together) |
| `undated_photo` | text | `"Photos Undated"` | album for photos with no confident date |
| `undated_video` | text | `"Videos Undated"` | album for videos with no confident date |
| `duplicates` | text | `"Duplicates (review)"` | album for byte-identical extra copies (moved, not deleted) |
| `soft_cap` | whole number (1 or more) | `4000` | split an album into '- Part N' above this many items |
| `hard_cap` | whole number (1 or more) | `5000` | never put more than this many items in one album |

## `[takeout]`: Google Takeout import (`gp2sm takeout`)

| key | type | default | meaning |
|---|---|---|---|
| `archives` | text | `"takeout"` | folder with the Google Takeout archives (.zip or .tgz) |
| `index` | text | `"takeout_index.db"` | Takeout index database |
| `heic` | one of "convert", "keep" | `"convert"` | HEIC photos: convert to JPEG here (EXIF kept) or upload as is (the destination may convert) |
| `live_clips` | one of "pair", "separate", "skip" | `"pair"` | Live Photo motion clips: pair (same name, next to the still), separate (video albums), skip |
| `unpaired_clips` | one of "dated", "undated", "skip" | `"dated"` | clips with no matching still: dated video album by their own time, the undated album, or skip |
| `rejected_types` | one of "skip", "convert" | `"skip"` | photos in formats the destination rejects (e.g. WebP, BMP): skip (listed in the report), or convert to JPEG |
| `dedupe` | one of "content", "exact", "off" | `"content"` | skip items already on the destination: exact (same bytes), content (also same picture), off |
| `existing` | list of text | `[]` | folders or albums (by name, e.g. "Family/2023-05") to check for existing copies; empty = this project's folder, ["/"] = the whole account |
| `same_max` | whole number (1 or more) | `6` | content check: picture distance at or below this is the same photo |
| `different_min` | whole number (1 or more) | `19` | content check: distance at or above this is a different photo; between = review |
| `pair_window` | whole number (1 or more) | `60` | a clip pairs with a still taken within this many seconds |
| `aspect_tolerance_pct` | whole number (1 or more) | `2` | a clip pairs only with a still of the same shape, within this percent |

## `[organize]`: Organizing items already on the destination (`gp2sm organize`)

| key | type | default | meaning |
|---|---|---|---|
| `sources` | list of text | `[]` | folders or albums (by name) whose items are organized, e.g. ["Uploads"] |
| `dates` | list of text | `["camera", "filename", "album"]` | where capture dates come from, in order: camera (the file's own date), filename, album (a date in the source album's name), upload (last resort) |
| `mode` | one of "move", "collect" | `"move"` | move items into the dated albums, or collect them (copies stay in the source; use for albums an uploader app still writes to) |
| `skip_newer_than_days` | whole number (0 or more) | `0` | leave items uploaded within this many days alone (0 = none) |
| `duplicates` | one of "park", "keep" | `"park"` | byte-identical extra copies: park in the duplicates album, or keep where they are |
| `unassigned` | text | `"Unassigned"` | group name for items no [[organize.group]] rule matches |
| `group` | list of rules | `[]` | ordered rules naming a group, e.g. [[organize.group]] name = "Phone" model = "iPhone*" (also: album, make, filename; case-insensitive globs) |

## `[naming]`: Album names (`gp2sm albums`)

| key | type | default | meaning |
|---|---|---|---|
| `scope` | list of text | `[]` | folders or albums (by name) whose album names are checked; ["/"] = the whole account |
| `exclude` | list of text | `[]` | album names to leave alone (case-insensitive globs), e.g. ["*Auto Upload*"] |
| `month` | album name template | `"{yyyy}-{mm} {subject}"` | new name when the date has a month |
| `year` | album name template | `"{yyyy} {subject}"` | new name when only the year is known |
| `day` | album name template | `"{yyyy}-{mm}-{dd} {subject}"` | new name when keep_day is on and the day is known |
| `keep_day` | one of true, false | `true` | keep the day when the old name has one (false drops it: less information) |
| `min_confidence` | one of "high", "medium" | `"high"` | apply renames this sure without review; the rest are listed |
| `date_from_photos` | one of true, false | `true` | date albums with no date in their name from a sample of photos |
| `photo_sample` | whole number (1 or more) | `60` | photos sampled per undated album (a few random pages) |
| `min_photos` | whole number (1 or more) | `5` | an undated album needs at least this many dated photos to be dated from them |
| `max_spread_days` | whole number (1 or more) | `45` | photos spread over more days than this don't date an album |

## `[run]`: Performance

| key | type | default | meaning |
|---|---|---|---|
| `move_batch_size` | whole number (1 or more) | `25` | items per batch move |
| `workers` | whole number (1 or more) | `8` | parallel network workers |

## `[[policy]]`: album settings policy (`gp2sm albums audit`, `fix`)

Ordered entries; for each setting the last matching entry wins. Each entry needs a `scope`
(folder/album globs matched against "folder/name" and the album name; `["*"]` = every album) and may
have an `exclude` list. Settings:

| setting | values |
|---|---|
| `privacy` | "public", "unlisted", "private" |
| `search` | "inherit", "no" |
| `web_search` | true, false |
| `downloads` | true, false |
| `download_size` | "medium", "large", "xlarge", "x2large", "x3large", "x4large", "x5large", "4k", "5k", "original" |
| `largest_size` | "medium", "large", "xlarge", "x2large", "x3large", "x4large", "x5large", "4k", "5k", "original" |
| `protected` | true, false |
| `watermark` | true, false |
| `share` | true, false |
| `comments` | true, false |
| `ranking` | true, false |
| `exif` | true, false |
| `filenames` | true, false |
| `geography` | true, false |
| `slideshow` | true, false |
| `printable` | true, false |
| `hide_owner` | true, false |
| `sort` | "position", "caption", "filename", "date_uploaded", "date_modified", "date_taken" |
| `sort_direction` | "ascending", "descending" |

`download_size` is only checked together with `downloads = true` (SmugMug resets it when downloads
are turned off). A setting a containing folder overrides (a Private folder makes everything in it
private) is reported, not changed.
