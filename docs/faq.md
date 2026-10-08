# FAQ

**What happens to Live Photos?**
Google Takeout keeps a Live Photo as a still (usually HEIC) plus a short video clip. gp2sm uploads the still as a JPEG and the clip as an MP4 next to it, under the same name, in an album sorted by file name, so each pair sits together. Clips are matched to their still by capture time and picture shape, not just by name, because Google sometimes swaps names between Live Photos. Clips with no matching still go to the video month album (or wherever `unpaired_clips` says). The SmugMug phone apps upload only the still of a Live Photo.

**Why convert HEIC to JPEG?**
SmugMug converts HEIC to JPEG itself, renaming `IMG_1.HEIC` to `IMG_1.JPG` and keeping no original. Converting first lets gp2sm keep all the camera data (dates, location, camera), fill in a missing date from the Takeout, and check duplicates reliably. Set `heic = "keep"` to upload HEIC as is.

**Which files does SmugMug refuse?**
WebP, BMP and ICO images, and very low-resolution videos. By default gp2sm skips those formats and lists them in the report (`rejected_types = "skip"`); set `rejected_types = "convert"` to convert them to JPEG instead. Rejected videos are reported as failed uploads.

**Is anything uploaded twice?**
Not on purpose. Before uploading, gp2sm compares every Takeout item with what's already in the albums in scope (`[takeout] existing`): identical files, the same picture re-encoded, and the same video are skipped. Re-running a step only adds new work. One known gap: a Live Photo clip with no matching still isn't checked against SmugMug (rare; the worst case is a duplicate clip).

**How big can an album get?**
SmugMug allows 5,000 items per album. gp2sm continues a month in `… - Part 2` after `soft_cap` items (4,000 by default) and never goes past `hard_cap`.

**My phone app uploads to SmugMug. Can gp2sm organize those?**
Yes, and that's what `gp2sm organize` is for. The iOS app already files uploads into month galleries. The Android app uploads into one album you choose (continuing in a numbered album when it fills), and those uploads are the usual thing to organize. Use `mode = "collect"` to leave the originals where the app put them.

**Does gp2sm change album links?**
No. Renames change the display name only; SmugMug never redirects an album's old address, so gp2sm doesn't touch it.

**Can I use it without Google Photos?**
Yes: `gp2sm organize` and `gp2sm albums` work on any SmugMug account. The import side is built on a general "source" interface, and Google Takeout is the one implemented so far.

**Does renaming check an album's date against its photos?**
Only when the name doesn't settle it. An album with no date in its name, only a year, or a range of years (like a school year, `2019/2020`) is dated from a sample of its photos, and only when they were taken close together. A name with a full date (`Snow 1-21-2016`, `March 10, 2005`) is trusted as it is: a typo in the name is carried into the new name. Look over `gp2sm albums report` before `apply`.

**Can gp2sm run on a schedule?**
gp2sm doesn't schedule itself; use your system's scheduler (cron, launchd, Task Scheduler) or run checks by hand. The check commands (`plan`, `verify`, and `albums audit`) exit with code **3** when they find something for a person to look at (drift from a policy, names to fix, items to organize, a mismatch), **0** when everything is in order, **130** when interrupted, and **1** on an error. A scheduled job can report or alert on exit code 3 and leave every change to a deliberate `--yes`.
