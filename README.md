# gp2sm - Google Photos to SmugMug Transfer Tool (v2.0)

A Python tool for transferring media (photos and videos) from Google Photos to SmugMug, featuring **parallel processing**, duplicate checking, HEIC handling options, automatic configuration file generation, enhanced authentication handling, colorized logging, graceful shutdown, SQLite-based state tracking for resuming interrupted transfers, configuration consistency checks, and automatic database renaming on successful completion.

## Important Notes

* This project is not affiliated with, endorsed by, or officially supported by Google™ or SmugMug®.
* Google Photos™ is a trademark of Google LLC.
* SmugMug® is a registered trademark of SmugMug, Inc.

## Features (v2.0)

* **Transfer photos and videos** from Google Photos to SmugMug.
* **Parallel Processing (v2.0):** Uses multiple worker threads (default: 5, configurable via `--workers`) to process items concurrently. Each worker handles the full lifecycle for an item (Download -> Hash -> Check -> Upload) sequentially before picking the next item. This significantly speeds up transfers, especially when dealing with many items or network latency.
* **SQLite Database State Tracking:**
    * Stores the list of items to be transferred and their processing status (PENDING, HASHED, UPLOADED_SUCCESS, DUPLICATE_HASH, ERROR_DOWNLOAD, etc.) in a local SQLite database (`gp2sm_transfer_state.db` by default).
    * Calculated MD5 hashes for images are stored to avoid re-hashing.
    * Allows the script to be stopped and resumed, picking up where it left off based on the database state.
    * Reduces redundant API calls to fetch the full list on every run after the first.
* **Automatic Error Retry (v2.0):** Items marked with an error status in previous runs are automatically included for reprocessing in subsequent runs.
* **Database Preservation:** Upon successful completion of the entire transfer (no errors in the final run and no items left pending), the database file is automatically renamed with a timestamp (e.g., `gp2sm_transfer_state_completed_YYYYMMDD_HHMMSS.db`) to preserve the final state for auditing and prevent accidental resumption.
* **Configuration Consistency Check:**
    * Stores a snapshot of the initial run's target configuration (SmugMug album/folder, Google source) in the database.
    * On resume, compares current settings against the stored snapshot.
    * Warns the user if a mismatch is detected and continues using the stored settings to ensure consistency for the in-progress transfer.
    * Provides instructions on how to override this behavior (`--force-refresh-list` or deleting the DB) if the user intends to change the target.
* **Duplicate Checking:**
    * Checks for existing images on SmugMug using MD5 hash comparison (hash is calculated once and stored in the database).
    * Checks for existing videos on SmugMug using filename comparison (case-insensitive).
* **HEIC File Handling:** Ignores Apple HEIC files by default, with an option to process them (converting them to JPGs on SmugMug, disabling duplicate checks for them).
* **Google Photos Album Support:** Optionally process only media items from a specific Google Photos album ID (used when initially populating the database).
* **SmugMug Folder/Album Management:**
    * Specify target album by name or by key/URI.
    * Optionally specify a target folder path (e.g., "Folder/SubFolder").
    * Automatically creates folders and/or albums if they don't exist.
* **Configuration Assistance:**
    * Generates a default `smugmug_config.json` if it's missing.
    * Adds missing required fields with placeholders to an existing `smugmug_config.json` if incomplete.
    * Provides clear instructions if the Google API credentials file (`google_api_keys.json`) is missing.
* **Improved Logging (v2.0):**
    * Colorized console output for different message levels (INFO, PROGRESS, WARNING, ERROR, DEBUG).
    * **Includes thread names** in log messages for easier debugging of parallel operations.
    * Detailed file logging to `gp2sm_transfer.log` (with log rotation).
    * Google Photos IDs are truncated in log messages for better readability.
    * **Logger-based progress updates** show overall progress and counts after each item completes.
* **Enhanced Authentication Handling:** Includes automatic refresh for expired Google Photos tokens and proactive refresh before expiry. Re-fetches Google Photos item details if download URLs expire during processing (details updated in DB).
* **Graceful Shutdown:** Handles Ctrl+C and termination signals to stop cleanly, allowing active workers to finish their current item before cleaning up resources (lock file, temporary directory, database connection).
* **Dry Run Mode:** Simulate the transfer process without uploading or modifying DB status beyond checks.
* **Selective Transfer:** Options to ignore photos or ignore videos during transfer.
* **Lock File:** Prevents multiple instances of the script from running simultaneously.
* **Token Storage:** Securely stores and reuses OAuth tokens locally (`google_photos_token.json`, `smugmug_config.json`) to minimize re-authentication.

## Prerequisites

* Python 3.7 or higher.
* pip (Python package installer).

## Setup

1.  **Clone or Download:** Get the script files (`main.py`, `google_photos_module.py`, `smugmug_module.py`, `database_manager.py`, `requirements.txt`, etc.).
2.  **Install Dependencies:** Open a terminal or command prompt in the script's directory and run:

        pip install -r requirements.txt
        # Or manually:
        # pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib requests requests_oauthlib colorlog

3.  **Configure Google Photos API:**
    * Follow the instructions in the [Google Cloud Console](https://console.cloud.google.com/) to create an OAuth 2.0 Client ID for **Desktop application**.
    * Enable the "Photos Library API" for your project.
    * Download the client secrets JSON file.
    * **IMPORTANT:** Rename the downloaded file to exactly `google_api_keys.json` and place it in the same directory as the script.
    * *(If you run the script and this file is missing, it will print detailed instructions to the console and log.)*
4.  **Configure SmugMug API:**
    * Register an application on the [SmugMug Developer Portal](https://api.smugmug.com/api/developer/apply) to get your API Key and Secret.
    * **Option 1 (Recommended):** Run the script once (`python main.py`). If `smugmug_config.json` doesn't exist, the script will create a default one for you and exit.
    * **Option 2 (Manual):** Copy `smugmug_config.json.example.txt` (if provided) to `smugmug_config.json`.
5.  **Edit `smugmug_config.json`:**
    * Fill in your obtained `api_key` and `api_secret`.
    * **Choose ONE method** to specify the target album:
        * Set `album_name` to your desired album name (e.g., "Google Photos Import"). Leave `album_key` and `album_api_uri` as placeholders.
        * OR, find the `AlbumKey` for an existing SmugMug album (usually in the album's URL) and update `album_key` and `album_api_uri`. Set `album_name` to its placeholder value (`"YOUR_SMUGMUG_ALBUM_NAME"`).
    * Optionally, set `folder_name` to a folder path (e.g., "My Photos/Google Imports"). Folders will be created if they don't exist.
    * Leave `oauth_token` and `oauth_token_secret` as placeholders; the script will handle authentication and save them automatically.
    * Optionally, set `process_heic` to `true` if you want to process HEIC files (see warnings below).
6.  **Running the Script & Authorization:**
    * Navigate: Open your terminal or command prompt to the directory containing `main.py`.
    * Run: Execute the script using:

            python main.py [OPTIONS]

        *(See Command-Line Arguments below for available `[OPTIONS]`)*
    * **First-Time Authorization:**
        * **Google Photos:** The script will open a web browser asking you to log in to your Google account and grant permission for the script to access your photos. Follow the prompts. After granting permission, tokens will be saved to `google_photos_token.json`.
        * **SmugMug:** The script will print a URL. Open this URL in your browser, log in to SmugMug, and authorize the application. SmugMug will provide a verifier code (usually 6 digits). Copy this code and paste it back into the terminal when prompted. The script will then save the access tokens into `smugmug_config.json`.
    * **Configuration Validation:** The script checks `smugmug_config.json` for required fields. If essential fields (like API keys or album specification) are missing or still have placeholder values, the script will update the file with the necessary placeholders/comments and exit, prompting you to edit the file.
    * **Initial Run & Database Population:** On the first successful run (after authorization and config validation), the script will:
        * Fetch the list of media items from Google Photos (entire library or specified album).
        * Create the SQLite database file (`gp2sm_transfer_state.db` by default).
        * Save a snapshot of the current target configuration (SmugMug album/folder, Google source) to the database.
        * Populate the database with the fetched items, marking them as `PENDING`.
        * Begin processing items from the database using parallel workers.
    * **Subsequent Runs (Resuming):**
        * The script checks the database file.
        * It compares the current SmugMug/Google target settings (from config/CLI) with the snapshot stored in the database.
        * If settings differ: A warning is displayed, and the script proceeds using the **stored settings** from the database to ensure consistency for the ongoing transfer. You will be advised to delete the database or use `--force-refresh-list` if you want to use the new settings.
        * If settings match: Processing resumes silently.
        * It queries the database for items that are not yet in a final state (e.g., `PENDING`, `HASHED`, `ERROR_*`).
        * Processing resumes from the items found in the database using parallel workers, skipping the initial list fetch from Google Photos.
    * **Completion:** When the script processes all items in the database without errors during the run and no items remain in a non-terminal state, it will:
        * Log a "All items processed successfully" message.
        * Rename the database file by appending `_completed_YYYYMMDD_HHMMSS` to its name (e.g., `gp2sm_transfer_state_completed_20250427_164530.db`). This preserves the final state and prevents the script from trying to resume this completed transfer on the next run. A new database will be created if you run the script again without the `--force-refresh-list` flag.
    * **Interrupting:** You can press `Ctrl+C` to request a graceful shutdown. The script will signal workers to stop submitting new tasks and attempt to finish processing active items before cleaning up and exiting. The database will store the state, allowing you to resume later.
    * **Logging:** Check the console (`stdout`) for colorized status messages (including progress updates) and review the `gp2sm_transfer.log` file for detailed logs (including thread names).
    * **Lock File:** A `gp2sm.lock` file is created while the script runs to prevent multiple instances. If the script crashes or is force-quit, you may need to delete this file manually before running again.

## Configuration Files

* `google_api_keys.json`: Your downloaded Google OAuth credentials. **(Required)**
* `google_photos_token.json`: Stores your Google Photos access/refresh tokens after authorization. (Generated by the script)
* `smugmug_config.json`: Stores your SmugMug API key/secret, OAuth tokens (filled in by script), and target album/folder settings. **(Required - edit after first run)**
* `gp2sm_transfer_state.db`: SQLite database tracking transfer progress. (Generated by the script)
* `gp2sm_transfer.log`: Detailed log file. (Generated by the script)
* `gp2sm.lock`: Prevents multiple script instances. (Generated by the script)

## Command-Line Arguments

* `--google-photos-album-id <ALBUM_ID>`: (Optional) Process only media items from the specified Google Photos album ID. Used only when initially populating the database (i.e., when the DB is empty or `--force-refresh-list` is used).
* `--delete-from-google`: (Optional) Simulate deleting items from Google Photos after successful upload/check. **Warning:** The Google Photos API does not currently support deletion, so this flag only logs what *would* be deleted. No actual deletion occurs.
* `--smugmug-album <ALBUM_NAME>`: (Optional) Specify the name of the target SmugMug album. Overrides the `album_name`, `album_key`, and `album_api_uri` settings in the config file. If the album doesn't exist in the target location, it will be created.
* `--smugmug-folder <FOLDER_PATH>`: (Optional) Specify the SmugMug folder path (e.g., "Vacations/Europe 2024") to place the album in. Overrides the `folder_name` setting in the config file. Folders in the path will be created if they don't exist.
* `--dry-run`: (Optional) Perform a dry run: check existence, log actions, but do not upload to SmugMug or modify DB status beyond checks. Downloads may still occur for MD5 checking.
* `--ignore-photos`: (Optional) Mark photos (images) as `SKIPPED_FILTER` in the database during processing.
* `--ignore-videos`: (Optional) Mark videos as `SKIPPED_FILTER` in the database during processing.
* `--process-heic`: (Optional) Process HEIC files (Apple Live Photos). Default is to ignore them. See "HEIC File Handling" below for important implications.
* `--db-file <PATH>`: (Optional) Specify the path to the SQLite database file. Defaults to `gp2sm_transfer_state.db` in the current directory.
* `--force-refresh-list`: (Optional) Ignore existing database content and re-fetch the complete list from Google Photos, populating the database again (including saving a new configuration snapshot). Use if you suspect the DB is out of sync or want to start a new transfer based on current settings. Existing entries won't be deleted, but new items will be added.
* `--reset-errors`: (Optional) Before starting the processing loop, reset all items currently marked with an error status back to `PENDING` for a fresh retry attempt.
* `--workers <NUM>`: (Optional) Specify the number of parallel worker threads to use (default: 5).
* `--debug`: (Optional) Enable debug logging, providing more verbose output to both the console and the log file.
* `--version`: Show the script's version number and exit.
* `-h`, `--help`: Show the help message and exit.

## How Duplicate Checking Works

* **Images:** The script downloads the image from Google Photos (if needed and not already hashed), calculates its MD5 hash, and stores the hash in the database. It then checks if any image in the target SmugMug album has a matching `ArchivedMD5` value. This prevents uploading exact duplicates.
* **Videos:** Because SmugMug re-encodes videos upon upload, their MD5 hash changes. Therefore, the script checks for existing videos by comparing the `FileName` from Google Photos (case-insensitive) against the filenames of items already in the target SmugMug album. This check happens before downloading the video (if possible) to save bandwidth if a filename match is found.
* **HEIC Files:** If `--process-heic` is enabled, no duplicate checking is performed for these files due to SmugMug's conversion process. They are marked as `SKIPPED_HEIC` or proceed directly to upload attempt.

## HEIC File Handling (Apple Live Photos)

* Apple Live Photos typically consist of a `.jpg` image and a `.mov` or `.heic` file containing the short video portion.
* Google Photos preserves both parts.
* SmugMug accepts `.heic` uploads but converts them into static `.jpg` images, changing the file extension and content. The 'live' video aspect is lost.
* Because of this conversion, it's impossible to reliably check for duplicates of `.heic` files on SmugMug based on filename or hash after they have been uploaded and converted.
* By default, this script **IGNORES** `.heic` files (marks them as `SKIPPED_HEIC` in the database).
* You can enable processing of `.heic` files using the `--process-heic` command-line flag or by setting `"process_heic": true` in `smugmug_config.json`.
* **If you enable HEIC processing, be aware that:**
    * The live video portion will be lost on SmugMug.
    * Duplicate checking for `.heic` files is completely skipped. You may end up with duplicate converted JPGs on SmugMug if you run the script multiple times with this option enabled.

## Status Codes (in Database)

* `PENDING`: Item added to DB, awaiting processing.
* `HASHED`: MD5 hash calculated (for images).
* `SMUGMUG_CHECKED_NOT_FOUND`: Checked SmugMug, no duplicate found. Ready for upload.
* `DOWNLOADED_FOR_UPLOAD`: (Intermediate state, less common in v1.9/v2.0) File downloaded.
* `UPLOAD_ATTEMPTED`: Upload request sent to SmugMug.
* `UPLOADED_SUCCESS`: Successfully uploaded to SmugMug.
* `DUPLICATE_HASH`: Image with the same MD5 hash already exists on SmugMug.
* `DUPLICATE_FILENAME`: Video with the same filename already exists on SmugMug.
* `SKIPPED_FILTER`: Skipped due to `--ignore-photos` or `--ignore-videos`.
* `SKIPPED_HEIC`: Skipped because it's a HEIC file and processing is disabled.
* `ERROR_DOWNLOAD`: Failed to download the item from Google Photos.
* `ERROR_HASHING`: Failed to calculate the MD5 hash.
* `ERROR_SMUGMUG_API`: Error interacting with the SmugMug API (check/upload).
* `ERROR_UPLOAD_FAILED`: SmugMug upload request failed after being sent.
* `ERROR_UNKNOWN`: An unexpected error occurred during processing.
* `ERROR_MISSING_DATA`: Item in DB lacks essential info (ID, filename, mimeType).

## Logging

* **Console:** Shows INFO level logs by default (use `--debug` for more detail). Uses colors for different log levels. Output is directed to `stdout`. Includes thread names and progress updates.
* **File:** `gp2sm_transfer.log` contains detailed DEBUG level logs (including thread names). The file rotates when it reaches 5MB, keeping up to 5 backup files.

## Lock File

The script creates `gp2sm.lock` while running to prevent accidental simultaneous executions, which could corrupt the database or cause API issues. If the script crashes or is force-quit, you may need to manually delete this file before running again.

## License

This project is released into the public domain under The Unlicense. See the `LICENSE` file for details.
