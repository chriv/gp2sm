# Requesting a Google Takeout of Google Photos

Google Takeout is the only way to get your whole Google Photos library out of Google. It always includes **everything** in Google Photos (you can't pick albums), with a metadata file next to each photo and video.

1. Go to [takeout.google.com](https://takeout.google.com) and sign in to the Google account that owns the photos.
2. Choose **Deselect all**, then tick only **Google Photos**.
3. Next step: choose how the archive is delivered (a download link by email is simplest), the frequency (export once), the file type (**.zip** or **.tgz**; gp2sm reads both) and the size (the largest size means fewer files to download).
4. Create the export. Google prepares it in the background, which can take hours or days for a large library, then emails you.
5. Download **every part** and put them all in your project's `takeout/` folder. Don't extract them: gp2sm reads the archives directly. A Live Photo's still and its clip can end up in different parts, which is fine as long as all parts are there.

Then run `gp2sm plan` (or `gp2sm takeout index` first if you just want to see what's in the export).

Tips:
- Large exports are big: a 35,000-item library was about 87 GB. Make sure the disk has room for the archives, plus a few GB for the converted files gp2sm stages before uploading.
- If a download fails, download just that part again. gp2sm indexes each archive once and skips the ones it has already read.
