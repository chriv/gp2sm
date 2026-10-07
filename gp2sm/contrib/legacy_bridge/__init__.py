"""Bridge for libraries first moved with the old gp2sm v1/v2 Google Photos API tool.

That tool left transfer databases (Google item ids, filenames, capture times, MD5s) and uploads whose bytes
differ from the Google Takeout originals. These commands link a Takeout to that history; new imports
don't need them (`gp2sm takeout …` decides what's already there from the destination itself).

  takeout_match   pair a Takeout index with the legacy items and the consolidation state  (gp2sm takeout-match)
  content_match   perceptual matching of unlinked Takeout stills to legacy uploads        (gp2sm content-match)
  takeout_upload  the legacy upload plan: Live Photo pairs and HEIC stills               (gp2sm takeout-upload)
"""
