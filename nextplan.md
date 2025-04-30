# Plan to Fix Album Rotation and Google Photos 429 Errors (gp2sm v2.0)

This plan addresses two primary issues:
1.  Flawed SmugMug album rotation logic leading to race conditions and incorrect distribution.
2.  Incorrect handling of Google Photos 429 (Quota Exceeded) errors, causing unwanted database updates and statistics inflation.

---

## Goal 1: Fix Album Rotation (Pre-Assignment Approach)

**Problem:** Race conditions cause incorrect application of the 80% rule and simultaneous creation of multiple new, nearly empty albums when capacity is hit.

**Approach:** Move the album selection logic from the worker threads to the main thread *before* tasks are submitted. The main thread will determine the target album for each item sequentially based on tracked counts, eliminating the race condition.

**Plan Steps:**

1.  **Initialize Album State (`main.py` - Before Worker Loop):**
    * Query the `smugmug_albums` table (via `db_manager.get_all_albums` or a new dedicated function) to get all known albums and their current `item_count`.
    * Identify the current target album:
        * Look for the album marked `is_current = 1`.
        * If none, find the most recently created album.
        * If no albums exist in the table, use the initial album name/key from config/args, call `smugmug.get_or_create_album_in_path`, add it to the `smugmug_albums` table via `db_manager.add_or_update_album` (marking it `is_current=True`), and fetch its initial count (likely 0).
    * Store the `current_album_key`, `current_album_name`, and `current_album_item_count` as variables in the main thread.
    * Define constants: `MAX_ALBUM_CAPACITY = 5000`, `ALBUM_THRESHOLD = int(MAX_ALBUM_CAPACITY * 0.80)`.

2.  **Pre-Assign Albums (`main.py` - Before Worker Loop):**
    * Create a new list or modify `items_to_process_list` to store the assigned target album key for each item.
    * Iterate through `items_to_process_list` sequentially:
        * For each `item`:
            * Check if `current_album_item_count >= ALBUM_THRESHOLD`.
            * If it is:
                * Log the intent to switch albums due to capacity.
                * Determine the `next_album_name` using `get_next_album_name(current_album_name)`.
                * Call `smugmug.get_or_create_album_in_path` to find/create the next album and get its `new_album_key` and `new_album_uri`. Handle potential failure (log error, stop assignment?).
                * Update `database_manager`: Call `add_or_update_album` to add the new album and mark it as `is_current=True`.
                * Update main thread variables: `current_album_key = new_album_key`, `current_album_name = next_album_name`, `current_album_item_count = 0` (or fetch the count if it might already exist with items).
                * Update the `run_config` table snapshot using `db_manager.save_config_snapshot` with the new current album details.
            * Assign the `current_album_key` to the `item` (e.g., `item['target_album_key'] = current_album_key`).
            * Increment the main thread's `current_album_item_count`.

3.  **Modify Worker Submission (`main.py`):**
    * When submitting tasks to the `ThreadPoolExecutor`, pass the `target_album_key` (e.g., `item_details['target_album_key']`) to the `process_item_worker` function.

4.  **Update Worker Logic (`process_item_worker` in `main.py`):**
    * Add `target_album_key` as a parameter to the function definition.
    * Use this `target_album_key` consistently when calling SmugMug functions that need an album scope (e.g., `smugmug.check_media_exists`, `smugmug.upload_media`). Ensure `upload_media` uses the `target_album_key`'s corresponding *URI* or is modified to accept the key directly.
    * **Remove** all calls to `check_album_capacity_and_switch` and `handle_album_full_switch` from the worker.
    * Upon successful upload (`STATUS_UPLOADED_SUCCESS`), ensure the worker calls `db_manager.increment_album_item_count(target_album_key)` to update the count for the specific album used.

5.  **Handle `SmugMugAlbumFullError` in Worker (`process_item_worker`):**
    * Keep the `try...except SmugMugAlbumFullError` block around the upload call.
    * If the error occurs:
        * Log a CRITICAL error indicating that the pre-assignment estimate was incorrect and the target album (`target_album_key`) is actually full.
        * Update the item status to `STATUS_ERROR_ALBUM_FULL` via `db_manager.update_item_status`.
        * Return `google_id, STATUS_ERROR_ALBUM_FULL`.
        * **Do not** attempt any album switching logic within the worker.

6.  **Database (`database_manager.py`):**
    * Verify the `smugmug_albums` table and its supporting functions (`get_all_albums`, `get_current_album`, `add_or_update_album`, `increment_album_item_count`, `get_album_item_count`) are robust.
    * Consider adding `get_album_counts()` to fetch all counts at once efficiently.
    * Remove the `is_album_near_capacity` function as it's no longer needed by workers.

---

## Goal 2: Fix Google Photos 429 Quota Handling

**Problem:** Quota errors trigger excessive, failing database updates and inflate error statistics, potentially causing secondary DB errors.

**Approach:** Prevent workers from attempting database updates or returning error statuses when a Google Photos operation fails specifically due to a 429 quota error (detected either directly or via the global `quota_exceeded_flag`). Ensure run statistics exclude these quota-related failures.

**Plan Steps:**

1.  **Google Photos Module (`google_photos_module.py`):**
    * Maintain the existing logic to `self.quota_flag.set()` when a 429 error is detected.
    * Ensure functions like `get_photos`, `get_media_item`, `download_photo` consistently return `None` or raise a specific custom exception when the failure is due to a 429 error. Returning `None` and checking the flag is likely sufficient.

2.  **Worker Logic (`process_item_worker` in `main.py`):**
    * **Before** calling any Google Photos API function (`download_photo`, `get_media_item`): Check if `quota_exceeded_flag.is_set()`. If yes, log a debug message and `return google_id, current_status` immediately (without DB update).
    * **After** a Google Photos API call fails (returns `None` or raises an error): Check if `quota_exceeded_flag.is_set()`.
        * If the flag is set, assume the failure was due to quota. Log this. **Do not** call `db_manager.update_item_status` with an error like `STATUS_ERROR_DOWNLOAD` or `STATUS_ERROR_QUOTA`. Return `google_id, current_status` (the status *before* the failed call).
        * If the flag is *not* set, proceed with normal error handling (e.g., update status to `STATUS_ERROR_DOWNLOAD`).
    * **Remove** any explicit calls to `db_manager.update_item_status(..., STATUS_ERROR_QUOTA, ...)` from the worker's logic or exception handlers.

3.  **Main Result Processing (`main.py` - `as_completed` loop):**
    * When processing a completed future's result (`google_id, final_status`):
        * Check if `quota_exceeded_flag.is_set()`.
        * **Do not increment `errors_in_run`** if `final_status` is an error status AND `quota_exceeded_flag.is_set()`. Also, do not increment `errors_in_run` if the returned `final_status` is `STATUS_ERROR_QUOTA`.
        * Log progress normally, but the cumulative error count (`current_total_errors` in the progress log) will not increment for these quota-related non-updates.

4.  **Final Summary (`main.py`):**
    * The "Errors this Run" count in the final summary should naturally reflect the logic from step 3 (excluding quota failures).
    * The "Overall Database Stats" should not show any items with `STATUS_ERROR_QUOTA` if the changes in step 2 were successful.
    * Retain the specific `CRITICAL` log message indicating the run stopped due to quota if `quota_exceeded_flag.is_set()` at the end.

5.  **SQLite Errors (Observation):**
    * The various SQLite errors observed in the logs during the quota error cascade are likely symptoms of the database connection being overwhelmed by rapid, concurrent failed update attempts.
    * **No direct changes** to `database_manager.py`'s transaction handling are planned initially. The significant reduction in database write attempts during quota errors (from Plan Step 2 above) is expected to resolve these secondary errors. Monitor logs after implementing the primary fix.