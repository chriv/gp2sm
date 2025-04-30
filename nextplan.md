# Plan to Fix Album Rotation and Google Photos 429 Errors (gp2sm v2.0) - Updated with Task List

This plan addresses two primary issues:
1.  Flawed SmugMug album rotation logic leading to race conditions and incorrect distribution.
2.  Incorrect handling of Google Photos 429 (Quota Exceeded) errors, causing unwanted database updates and statistics inflation.

---

## Goal 1: Fix Album Rotation (Pre-Assignment Approach)

**Problem:** Race conditions cause incorrect application of the 80% rule and simultaneous creation of multiple new, nearly empty albums when capacity is hit.

**Approach:** Move the album selection logic from the worker threads to the main thread *before* tasks are submitted. The main thread will determine the target album for each item sequentially based on tracked counts, eliminating the race condition.

**Plan Steps (Summary):**

1.  **Initialize Album State (`main.py` - Before Worker Loop):** Query DB for albums/counts, determine current album, define thresholds.
2.  **Pre-Assign Albums (`main.py` - Before Worker Loop):** Iterate through items, assign `target_album_key`, handle album switches sequentially in the main thread.
3.  **Modify Worker Submission (`main.py`):** Pass `target_album_key` to the worker function.
4.  **Update Worker Logic (`process_item_worker` in `main.py`):** Use assigned key, remove old switching logic, handle `SmugMugAlbumFullError` locally, increment count on success.
5.  **Database (`database_manager.py`):** Ensure necessary album tracking functions exist, remove obsolete `is_album_near_capacity`.

---

## Goal 2: Fix Google Photos 429 Quota Handling

**Problem:** Quota errors trigger excessive, failing database updates and inflate error statistics, potentially causing secondary DB errors.

**Approach:** Prevent workers from attempting database updates or returning error statuses when a Google Photos operation fails specifically due to a 429 quota error (detected either directly or via the global `quota_exceeded_flag`). Ensure run statistics exclude these quota-related failures.

**Plan Steps (Summary):**

1.  **Google Photos Module (`google_photos_module.py`):** Ensure consistent 429 detection, flag setting, and return values (`None` or specific exception). (Likely no change needed based on current code).
2.  **Worker Logic (`process_item_worker` in `main.py`):** Check global `quota_exceeded_flag` before GP calls and after GP failures. If quota is the reason, return the item's *current* status immediately without updating the DB. Remove explicit `STATUS_ERROR_QUOTA` DB updates.
3.  **Main Result Processing (`main.py` - `as_completed` loop):** Modify logic to *not* count errors towards `errors_in_run` if the `quota_exceeded_flag` was set when the error occurred or if the status is `STATUS_ERROR_QUOTA`.
4.  **Final Summary (`main.py`):** Ensure summary reflects adjusted error count and retains quota warning messages.
5.  **SQLite Errors (Observation):** Expect secondary DB errors to resolve once unnecessary updates during quota errors are eliminated. No direct DB transaction changes planned initially.

---

## Implementation Task List

Here is the plan broken down into sequential tasks. For each task, please provide the current source file(s) for modification. The expected output for each task is a **single diff file** (`.diff` or `.patch`) containing all changes made in that task.

1.  **Task 1: Database Preparation for Pre-Assignment**
    * **Objective:** Modify `database_manager.py` to support the main thread's album management. Ensure functions for getting all albums/counts and incrementing specific album counts are robust. Remove the no-longer-needed `is_album_near_capacity` function.
    * **Files Modified:** `database_manager.py`
    * **Expected Output:** Diff file for `database_manager.py`.

2.  **Task 2: Initialize Album State in Main Thread**
    * **Objective:** Add logic to `main.py` (before the worker submission loop) to query the database for existing albums/counts, determine the initial `current_album_key`, `current_album_name`, `current_album_item_count`, and define capacity constants (`MAX_ALBUM_CAPACITY`, `ALBUM_THRESHOLD`).
    * **Files Modified:** `main.py`
    * **Expected Output:** Diff file for `main.py`.

3.  **Task 3: Implement Pre-Assignment Loop in Main Thread**
    * **Objective:** Add the loop in `main.py` (before worker submission) that iterates through `items_to_process_list`, checks `current_album_item_count` against `ALBUM_THRESHOLD`, performs sequential album switching logic if needed (find/create next album, update DB tables `smugmug_albums` and `run_config`, update main thread state variables), and assigns the determined `target_album_key` to each item dictionary.
    * **Files Modified:** `main.py`
    * **Expected Output:** Diff file for `main.py`.

4.  **Task 4: Pass Target Album Key to Worker**
    * **Objective:** Modify the `process_item_worker` function definition in `main.py` to accept `target_album_key` as a parameter. Update the `executor.submit` call to pass this value from the item dictionary prepared in Task 3.
    * **Files Modified:** `main.py`
    * **Expected Output:** Diff file for `main.py`.

5.  **Task 5: Update Worker Logic for Pre-Assigned Album**
    * **Objective:** Modify the body of `process_item_worker` in `main.py`. Replace uses of the global/shared `smugmug.album_key` with the passed-in `target_album_key` for SmugMug operations. Remove calls to `check_album_capacity_and_switch` and `handle_album_full_switch`. Update the `SmugMugAlbumFullError` handler to log the error and return `STATUS_ERROR_ALBUM_FULL` without attempting a switch. Ensure successful uploads call `db_manager.increment_album_item_count(target_album_key)`.
    * **Files Modified:** `main.py`
    * **Expected Output:** Diff file for `main.py`.

6.  **Task 6: Implement Quota Handling in Worker**
    * **Objective:** Modify `process_item_worker` in `main.py`. Add checks for `quota_exceeded_flag.is_set()` before calling Google Photos functions. After failed GP calls, check the flag again. If the flag is set, return the item's previous status (`current_status`) immediately without calling `db_manager.update_item_status`. Remove any explicit code that updates status to `STATUS_ERROR_QUOTA`.
    * **Files Modified:** `main.py`
    * **Expected Output:** Diff file for `main.py`.

7.  **Task 7: Adjust Quota Error Statistics**
    * **Objective:** Modify the result processing loop (`as_completed`) in `main.py`. Prevent the `errors_in_run` counter from being incremented if an error status is returned *and* the `quota_exceeded_flag` is set, or if the status is `STATUS_ERROR_QUOTA`. Ensure the final summary report uses this adjusted count.
    * **Files Modified:** `main.py`
    * **Expected Output:** Diff file for `main.py`.