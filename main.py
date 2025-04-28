# Google Photos to SmugMug Transfer Script (gp2sm) - v2.0
# Implements parallel processing using worker threads.
# - Handles SmugMug album full errors by switching albums live without restart.
# - Handles Google Photos Quota Exceeded errors by exiting gracefully.
# - Uses !children for folder/album checks in smugmug_module.
# - Includes debug logging for SmugMug API responses.
# - Progress indicator shows overall progress and overall stats.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload

__version__ = "2.0"

# Standard library imports
import argparse
import logging
import os
import sys
import time
import json
import datetime
import re
from logging.handlers import RotatingFileHandler
import signal
import queue
import concurrent.futures
import threading
import traceback

# Third-party imports
import colorlog

# Local module imports
# Pass the quota_exceeded_flag to GooglePhotos constructor
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug, DEFAULT_SMUGMUG_CONFIG, SmugMugAlbumFullError
from database_manager import (
    DatabaseManager, DB_FILE_DEFAULT, MEDIA_TABLE_NAME, CONFIG_TABLE_NAME,
    STATUS_PENDING, STATUS_HASHED, STATUS_SMUGMUG_CHECKED_NOT_FOUND,
    STATUS_DOWNLOADED_FOR_UPLOAD, STATUS_UPLOAD_ATTEMPTED, STATUS_UPLOADED_SUCCESS,
    STATUS_DUPLICATE_HASH, STATUS_DUPLICATE_FILENAME, STATUS_SKIPPED_FILTER,
    STATUS_SKIPPED_HEIC, STATUS_ERROR_DOWNLOAD, STATUS_ERROR_HASHING,
    STATUS_ERROR_SMUGMUG_API, STATUS_ERROR_UPLOAD_FAILED,
    STATUS_ERROR_ALBUM_FULL, STATUS_ERROR_QUOTA, # Import new status
    STATUS_ERROR_UNKNOWN, STATUS_ERROR_MISSING_DATA,
    TERMINAL_STATUSES, ERROR_STATUSES
)

# --- Constants ---
LOG_FILE = "gp2sm_transfer.log"
LOCK_FILE = "gp2sm.lock"
LOG_ID_TRUNCATE_LEN = 8
MAX_WORKERS = 5
PROGRESS_LEVEL_NUM = 15

# --- Global Variables ---
logger = None
shutdown_requested = False
google_photos_instance_global = None
db_manager_global = None
shutdown_event = threading.Event()
album_switch_lock = threading.Lock()
quota_exceeded_flag = threading.Event() # Flag for Google Quota
# Counters for run-specific summary (used internally for increments)
uploaded_in_run = 0
duplicates_in_run = 0
skipped_in_run = 0
errors_in_run = 0

# --- Signal Handling ---
def signal_handler(sig, frame):
    """Handles termination signals (Ctrl+C, etc.) for graceful shutdown."""
    global shutdown_requested, logger, shutdown_event, quota_exceeded_flag
    if not shutdown_requested:
        signal_name = f"Signal {sig}"
        try:
            signal_name = signal.Signals(sig).name
        except ValueError:
            pass
        log_func = getattr(logger, 'warning', lambda msg: print(msg, file=sys.stderr))
        log_func(f"Received signal {signal_name}. Initiating graceful shutdown...")
        log_func(f">>> Signal {signal_name} received. Stopping submission of new tasks... <<<")
        shutdown_requested = True
        shutdown_event.set()
        quota_exceeded_flag.set() # Also set quota flag on manual shutdown for consistent exit msg
    else:
        if logger:
            logger.debug(f"Shutdown already in progress. Received signal {sig} again.")
        else:
             print(">>> Shutdown already requested. Please wait. <<<", file=sys.stderr)


# --- Logging Setup ---
def setup_logging(debug=False):
    """Configures logging to console (stdout) and a rotating file."""
    global logger, PROGRESS_LEVEL_NUM
    logging.addLevelName(PROGRESS_LEVEL_NUM, "PROGRESS")
    def progress(self, message, *args, **kws):
        if self.isEnabledFor(PROGRESS_LEVEL_NUM):
            self._log(PROGRESS_LEVEL_NUM, message, args, **kws)
    logging.Logger.progress = progress

    log_level = logging.DEBUG if debug else logging.INFO
    if log_level > PROGRESS_LEVEL_NUM:
        log_level = PROGRESS_LEVEL_NUM

    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(log_level)

    console_format = ('%(log_color)s%(asctime)s - %(levelname)-8s - '
                      '[%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - '
                      '%(message)s')
    console_formatter = colorlog.ColoredFormatter(
        console_format, datefmt='%Y-%m-%d %H:%M:%S', reset=True,
        log_colors={
            'DEBUG':    'cyan', 'INFO':     'white', 'PROGRESS': 'blue',
            'WARNING':  'yellow', 'ERROR':    'red', 'CRITICAL': 'red,bg_white',
        }, style='%'
    )
    console_handler = colorlog.StreamHandler(sys.stdout) # Ensure stdout
    console_handler.setFormatter(console_formatter)
    console_handler_level = logging.DEBUG if debug else logging.INFO
    if console_handler_level > PROGRESS_LEVEL_NUM:
         console_handler_level = PROGRESS_LEVEL_NUM
    console_handler.setLevel(console_handler_level)
    logger.addHandler(console_handler)

    file_format = '%(asctime)s - %(levelname)-8s - [%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - %(message)s'
    file_formatter = logging.Formatter(file_format, datefmt='%Y-%m-%d %H:%M:%S')
    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception as e:
        # Use print here as logger might not be fully set up
        print(f"Warning: Could not configure file logging to '{LOG_FILE}': {e}", file=sys.stderr)
        if logger: # Log if logger exists partially
            logger.error(f"Failed file logging handler setup: {e}", exc_info=True)

    # Reduce verbosity of underlying libraries
    for lib_logger_name in ["googleapiclient.discovery_cache", "google.auth.transport.requests",
                            "urllib3.connectionpool", "requests_oauthlib.oauth1_session"]:
        logging.getLogger(lib_logger_name).setLevel(logging.WARNING)

    # Set level for our modules based on debug flag
    logging.getLogger("database_manager").setLevel(log_level)
    logging.getLogger("google_photos_module").setLevel(log_level)
    logging.getLogger("smugmug_module").setLevel(log_level)


# --- Lock File Management ---
def acquire_lock():
    """Creates a lock file to prevent multiple instances."""
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        if logger:
            logger.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        log_msg = f"Lock file '{LOCK_FILE}' exists. Another instance running?"
        if logger:
            logger.error(log_msg)
        else:
            # Use print if logger failed init
            print(f"Error: {log_msg}", file=sys.stderr)
        return False
    except OSError as e:
        log_msg = f"Error acquiring lock file '{LOCK_FILE}': {e}"
        if logger:
            logger.error(log_msg, exc_info=True)
        else:
            print(f"Error: {log_msg}", file=sys.stderr)
        return False

def release_lock():
    """Removes the lock file."""
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            if logger:
                logger.info(f"Released lock file: {LOCK_FILE}")
        except OSError as e:
            log_func_warn = getattr(logger, 'warning', lambda msg, exc_info: print(f"Warning: {msg}", file=sys.stderr))
            log_func_warn(f"Could not remove lock file '{LOCK_FILE}': {e}", exc_info=True)


# --- Cleanup Function ---
def cleanup(google_photos_instance, db_manager_instance):
    """Performs cleanup actions."""
    log_func_info = getattr(logger, 'info', lambda msg: print(f"INFO: {msg}", file=sys.stderr))
    log_func_debug = getattr(logger, 'debug', lambda msg: print(f"DEBUG: {msg}", file=sys.stderr))
    log_func_error = getattr(logger, 'error', lambda msg, exc_info: print(f"ERROR: {msg}", file=sys.stderr))

    log_func_info("--- Running cleanup procedures ---")

    if db_manager_instance and hasattr(db_manager_instance, 'close'):
         try:
             log_func_debug("Closing database...")
             db_manager_instance.close()
         except Exception as e:
             log_func_error(f"Error closing DB: {e}", exc_info=True)
    else:
         log_func_debug("No DB manager for cleanup.")

    if google_photos_instance and hasattr(google_photos_instance, 'cleanup_temp_dir'):
        try:
            log_func_debug("Cleaning Google Photos temp dir...")
            google_photos_instance.cleanup_temp_dir()
        except Exception as e:
            log_func_error(f"Error cleaning GP temp dir: {e}", exc_info=True)
    else:
        log_func_debug("No GP instance for cleanup.")

    release_lock()
    log_func_info("--- Cleanup complete ---")


# --- Album Switching Logic ---
def get_next_album_name(current_album_name):
    """Generates the next sequential album name."""
    match = re.search(r" - Part (\d+)$", current_album_name, re.IGNORECASE)
    if match:
        part_number = int(match.group(1))
        base_name = current_album_name[:match.start()]
        next_part_number = part_number + 1
        return f"{base_name} - Part {next_part_number}"
    else:
        # If no ' - Part X' found, start with Part 2
        return f"{current_album_name} - Part 2"

def handle_album_full_switch(smugmug_instance, db_manager_instance, initial_album_name, current_folder_name, google_album_id):
    """
    Handles switching to the next SmugMug album when full, updating shared state live.
    Uses locking to ensure only one worker performs the switch action for a given album key.
    Relies on DB config snapshot as the source of truth for the *intended* target.
    Returns True if the switch was successfully performed by THIS call, False otherwise.
    """
    global album_switch_lock # Only need the lock globally

    with album_switch_lock:
        # 1. Get the *current* target album key from the database config snapshot
        stored_config = db_manager_instance.get_config_snapshot()
        if not stored_config:
            logger.error("Cannot perform album switch: Failed to retrieve stored config from DB.")
            return False # Indicate switch failed

        db_target_key = stored_config.get('current_album_key')

        # 2. Get the album key the shared smugmug object is *currently* using
        shared_target_key = smugmug_instance.album_key
        shared_target_name = smugmug_instance.album_name # Get the name of the album that filled up

        # 3. Compare DB target with shared object target
        if db_target_key != shared_target_key:
            # If they differ, it means another worker already successfully completed
            # the switch (updated DB and shared object). This worker just needs to retry.
            logger.debug(f"Album switch from {shared_target_key} to {db_target_key} already completed by another worker.")
            return False # Indicate switch already done

        # 4. If keys match, THIS worker is the first to handle the full error *for this album*
        logger.warning("="*60)
        logger.warning(f"SmugMug album '{shared_target_key}' ({shared_target_name or 'Name Unknown'}) reported as full! Attempting live switch...")

        # Use the album name currently associated with the shared instance
        current_full_album_name = shared_target_name
        if not current_full_album_name:
            # Fallback if name is missing for some reason, try using the key
            current_full_album_name = shared_target_key
            logger.warning(f"Album name missing for key {shared_target_key}, using key itself to generate next name.")
            if not current_full_album_name: # Should not happen if shared_target_key exists
                 logger.error("Cannot determine current full album name/key to generate next sequential name.")
                 return False

        next_album_name = get_next_album_name(current_full_album_name)
        logger.warning(f"Attempting to find/create next album: '{next_album_name}'")

        # Use a temporary SmugMug object instance for the get_or_create call
        temp_smugmug = SmugMug()
        temp_smugmug.config = smugmug_instance.config
        temp_smugmug.auth_session = smugmug_instance.auth_session
        temp_smugmug.user_uri = smugmug_instance.user_uri

        # Attempt to find or create the next album
        if temp_smugmug.get_or_create_album_in_path(next_album_name, current_folder_name):
            new_album_key = temp_smugmug.album_key
            new_album_uri = temp_smugmug.album_api_uri
            logger.info(f"Successfully found/created next album: '{next_album_name}' (Key: {new_album_key})")

            # Update the database config first (most critical)
            # Keep the original initial_album_name, update current key/uri
            if not db_manager_instance.save_config_snapshot(
                initial_album_name=initial_album_name, # Keep original base name
                album_key=new_album_key,             # NEW key
                album_uri=new_album_uri,             # NEW uri
                folder_name=current_folder_name,
                google_album_id=google_album_id
            ):
                logger.error("CRITICAL: Failed to update database config snapshot with new album!")
                return False # Indicate switch failed critically

            # Now, update the shared SmugMug instance state
            logger.warning(f"Updating shared SmugMug instance to target new album: {new_album_key}")
            smugmug_instance.album_key = new_album_key
            smugmug_instance.album_api_uri = new_album_uri
            smugmug_instance.album_name = next_album_name # Update name as well

            logger.warning("Live album switch complete. Subsequent uploads in this run will target the new album.")
            return True # Indicate switch was performed successfully by this call
        else:
            logger.error(f"Failed to find or create the next album '{next_album_name}'. Cannot switch.")
            return False # Indicate switch failed
# --- End Album Switching Logic ---


# --- Item Processing Worker Function (Modified Quota Handling) ---
def process_item_worker(item_details, google_photos, smugmug, db_manager, args):
    """Worker function: Download -> Hash -> Check -> Upload. Handles Quota."""
    global shutdown_event, album_switch_lock, quota_exceeded_flag # Access globals

    google_id = item_details.get('google_id')
    filename = item_details.get('filename')
    mime_type = item_details.get('mime_type')
    current_md5_hash = item_details.get('md5_hash')
    current_status = item_details.get('status', STATUS_PENDING)

    is_video = mime_type.startswith('video/') if mime_type else False
    is_heic = filename.lower().endswith(('.heic', '.heif')) if filename else False
    should_process_heic = args.process_heic or (smugmug.config and smugmug.config.get('process_heic', False))

    if not google_id or not filename or not mime_type:
        logger.error(f"Worker skip: missing core data: ID={google_id}, File={filename}, Mime={mime_type}")
        if google_id:
            db_manager.update_item_status(google_id, STATUS_ERROR_MISSING_DATA, "Missing filename/mimeType")
        return google_id, STATUS_ERROR_MISSING_DATA

    truncated_id = f"{google_id[:LOG_ID_TRUNCATE_LEN]}...{google_id[-LOG_ID_TRUNCATE_LEN:]}" if len(google_id) > LOG_ID_TRUNCATE_LEN * 2 else google_id
    log_identifier = f"Worker (ID: {truncated_id}, File: '{filename}')"

    temp_file_path = None
    final_status = current_status
    refreshed_details = None
    upload_needed = False

    try:
        # Check shutdown flags at the beginning
        if quota_exceeded_flag.is_set():
            logger.warning(f"{log_identifier}: Google Quota exceeded previously. Skipping.")
            # Ensure status reflects quota error if not already set
            if current_status != STATUS_ERROR_QUOTA:
                 db_manager.update_item_status(google_id, STATUS_ERROR_QUOTA, "Google API Quota Exceeded (detected on worker start)")
            return google_id, STATUS_ERROR_QUOTA
        if shutdown_event.is_set():
            logger.warning(f"{log_identifier}: Shutdown signalled pre-start. Skipping.")
            return google_id, final_status # Return current status if shutdown before start

        # --- Filters ---
        if args.ignore_photos and not is_video:
            logger.info(f"{log_identifier}: Skip (Photo filter).")
            db_manager.update_item_status(google_id, STATUS_SKIPPED_FILTER, "Skipped --ignore-photos")
            return google_id, STATUS_SKIPPED_FILTER
        if args.ignore_videos and is_video:
            logger.info(f"{log_identifier}: Skip (Video filter).")
            db_manager.update_item_status(google_id, STATUS_SKIPPED_FILTER, "Skipped --ignore-videos")
            return google_id, STATUS_SKIPPED_FILTER
        if is_heic and not should_process_heic:
            logger.info(f"{log_identifier}: Skip (HEIC disabled).")
            db_manager.update_item_status(google_id, STATUS_SKIPPED_HEIC, "HEIC not enabled")
            return google_id, STATUS_SKIPPED_HEIC

        # --- Initial Download Check ---
        needs_initial_download = is_video or (is_heic and should_process_heic) or \
                                (not is_video and not is_heic and not current_md5_hash)
        if needs_initial_download:
            logger.debug(f"{log_identifier}: Initial download needed...")
            temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details)
            if not temp_file_path:
                logger.error(f"{log_identifier}: Initial download failed.")
                # Check if failure was due to quota
                if quota_exceeded_flag.is_set():
                     logger.error(f"{log_identifier}: Download failed due to Google API Quota Exceeded.")
                     db_manager.update_item_status(google_id, STATUS_ERROR_QUOTA, "Google API Quota Exceeded")
                     shutdown_event.set() # Signal graceful shutdown
                     return google_id, STATUS_ERROR_QUOTA
                else:
                     db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Initial download failed")
                     return google_id, STATUS_ERROR_DOWNLOAD
            if refreshed_details:
                 logger.debug(f"{log_identifier}: Details refreshed. Updating DB.")
                 new_base_url = refreshed_details.get('baseUrl')
                 new_metadata = refreshed_details.get('mediaMetadata')
                 new_metadata_json = json.dumps(new_metadata) if new_metadata else item_details.get('media_metadata_json')
                 db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                 refreshed_details = None # Clear after use

        # --- Hashing ---
        if not is_video and not is_heic and not current_md5_hash:
            if not temp_file_path or not os.path.exists(temp_file_path):
                 logger.error(f"{log_identifier}: Temp file missing for hash.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Temp file missing pre-hash")
                 return google_id, STATUS_ERROR_DOWNLOAD
            logger.info(f"{log_identifier}: Calculating MD5...")
            calculated_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
            if not calculated_hash:
                logger.error(f"{log_identifier}: MD5 calc failed.")
                db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, "MD5 calc failed")
                return google_id, STATUS_ERROR_HASHING
            else:
                logger.debug(f"{log_identifier}: MD5: {calculated_hash}. Updating DB.")
                current_md5_hash = calculated_hash
                db_manager.update_item_status(google_id, STATUS_HASHED, md5_hash=current_md5_hash)
                final_status = STATUS_HASHED

        # --- Duplicate Check ---
        if not (is_heic and should_process_heic):
            target_album_key = smugmug.album_key # Use current key from shared object
            if not target_album_key:
                 logger.error(f"{log_identifier}: SM album key missing for dup check.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, "SM key missing for dup check")
                 return google_id, STATUS_ERROR_SMUGMUG_API

            logger.info(f"{log_identifier}: Checking SM album '{target_album_key}' for duplicates...")
            check_hash = current_md5_hash if not is_video else None
            exists = smugmug.check_media_exists(target_album_key, filename, mime_type, file_hash=check_hash)

            if exists:
                 dup_status = STATUS_DUPLICATE_FILENAME if is_video else STATUS_DUPLICATE_HASH
                 log_reason = "filename" if is_video else "MD5"
                 logger.info(f"{log_identifier}: Found on SM ({log_reason}). Marking duplicate.")
                 db_manager.update_item_status(google_id, dup_status, f"Duplicate check via {log_reason}")
                 final_status = dup_status
                 if args.delete_from_google:
                     google_photos.remove_photo(google_id, dry_run=args.dry_run)
                 return google_id, final_status
            else:
                 log_reason = "filename" if is_video else "MD5"
                 logger.info(f"{log_identifier}: Checked SM via {log_reason}: Not found.")
                 db_manager.update_item_status(google_id, STATUS_SMUGMUG_CHECKED_NOT_FOUND, f"SM check via {log_reason} - not found")
                 final_status = STATUS_SMUGMUG_CHECKED_NOT_FOUND
                 upload_needed = True
        else:
             logger.info(f"{log_identifier}: Skipping duplicate check for HEIC.")
             final_status = STATUS_PENDING
             upload_needed = True

        # --- Upload Step ---
        if upload_needed:
            if args.dry_run:
                logger.info(f"{log_identifier}: [DRY RUN] Would upload.")
                final_status = STATUS_SMUGMUG_CHECKED_NOT_FOUND
                if args.delete_from_google:
                    google_photos.remove_photo(google_id, dry_run=True)
                return google_id, final_status

            # Download if needed before upload
            if not temp_file_path or not os.path.exists(temp_file_path):
                logger.info(f"{log_identifier}: Downloading before upload...")
                temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details)
                if not temp_file_path:
                    logger.error(f"{log_identifier}: Download failed pre-upload.")
                    # Check if failure was due to quota
                    if quota_exceeded_flag.is_set():
                         logger.error(f"{log_identifier}: Download failed due to Google API Quota Exceeded.")
                         db_manager.update_item_status(google_id, STATUS_ERROR_QUOTA, "Google API Quota Exceeded")
                         shutdown_event.set() # Signal graceful shutdown
                         return google_id, STATUS_ERROR_QUOTA
                    else:
                         db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Download failed pre-upload")
                         return google_id, STATUS_ERROR_DOWNLOAD
                if refreshed_details:
                    logger.debug(f"{log_identifier}: Details refreshed pre-upload. Updating DB.")
                    new_base_url = refreshed_details.get('baseUrl')
                    new_metadata = refreshed_details.get('mediaMetadata')
                    new_metadata_json = json.dumps(new_metadata) if new_metadata else item_details.get('media_metadata_json')
                    db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                    refreshed_details = None

            # Get current target URI from shared object
            target_album_uri = smugmug.album_api_uri
            if not target_album_uri:
                 logger.error(f"{log_identifier}: SM target album URI missing. Cannot upload.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, "SM URI missing for upload")
                 return google_id, STATUS_ERROR_SMUGMUG_API

            logger.info(f"{log_identifier}: Uploading to SM album URI: {target_album_uri}...")
            db_manager.update_item_status(google_id, STATUS_UPLOAD_ATTEMPTED, increment_attempt=True)
            final_status = STATUS_UPLOAD_ATTEMPTED

            try:
                upload_success = smugmug.upload_media(target_album_uri, temp_file_path, filename, mime_type)
                temp_file_path = None # Consumed by upload_media

                if upload_success:
                    logger.info(f"{log_identifier}: Upload successful.")
                    db_manager.update_item_status(google_id, STATUS_UPLOADED_SUCCESS)
                    final_status = STATUS_UPLOADED_SUCCESS
                    if args.delete_from_google:
                        google_photos.remove_photo(google_id, dry_run=False)
                else:
                    logger.error(f"{log_identifier}: Upload failed (returned False).")
                    db_manager.update_item_status(google_id, STATUS_ERROR_UPLOAD_FAILED, "Upload function returned False")
                    final_status = STATUS_ERROR_UPLOAD_FAILED

            except SmugMugAlbumFullError as afe:
                 logger.error(f"{log_identifier}: Upload failed - SmugMug Album Full: {afe}")
                 # Update status FIRST to ERROR_ALBUM_FULL
                 db_manager.update_item_status(google_id, STATUS_ERROR_ALBUM_FULL, f"Album full: {afe}")
                 final_status = STATUS_ERROR_ALBUM_FULL
                 temp_file_path = None # Ensure path is None

                 # Attempt the album switch (function handles locking)
                 stored_config = db_manager.get_config_snapshot()
                 initial_base_name = stored_config.get('initial_album_name') if stored_config else "UnknownAlbumBase"
                 current_folder = stored_config.get('current_folder_name') if stored_config else None
                 google_source_id = stored_config.get('google_album_id') if stored_config else None

                 # Call the switch handler - it returns True only if THIS call performed the switch
                 switch_performed = handle_album_full_switch(
                     smugmug, db_manager, initial_base_name, current_folder, google_source_id
                 )
                 if switch_performed:
                     logger.info(f"{log_identifier}: Successfully initiated album switch.")
                 else:
                     logger.info(f"{log_identifier}: Album switch handled by another worker or failed. Item marked for retry.")

                 # Return ERROR_ALBUM_FULL so item is retried later

        return google_id, final_status # Return the final status determined

    except Exception as e:
        logger.error(f"Unexpected exception in worker for {google_id} ('{filename}'): {e}\n{traceback.format_exc()}")
        final_status = STATUS_ERROR_UNKNOWN
        try:
             error_msg_short = str(e)[:200]
             db_manager.update_item_status(google_id, final_status, f"Worker exception: {error_msg_short}")
        except Exception as db_e:
             logger.error(f"Failed update DB status after worker exception for {google_id}: {db_e}")
        return google_id, final_status
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
             try:
                 os.remove(temp_file_path)
                 logger.debug(f"{log_identifier}: Cleaned temp file in finally block.")
             except OSError as clean_e:
                 logger.warning(f"{log_identifier}: Failed clean temp file {temp_file_path}: {clean_e}")

# --- Main Function (Modified Quota Handling) ---
def main():
    """Main execution function."""
    global logger, shutdown_requested, google_photos_instance_global, db_manager_global, shutdown_event
    global uploaded_in_run, duplicates_in_run, skipped_in_run, errors_in_run
    global album_switch_lock, quota_exceeded_flag # Include quota flag

    parser = argparse.ArgumentParser(description="Transfer Google Photos to SmugMug.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # Args remain the same...
    parser.add_argument('--google-photos-album-id', help='(Optional) Google Photos Album ID to sync (syncs entire library if omitted).')
    parser.add_argument('--ignore-photos', action='store_true', help='Skip processing photos (only process videos).')
    parser.add_argument('--ignore-videos', action='store_true', help='Skip processing videos (only process photos).')
    parser.add_argument('--process-heic', action='store_true', help='Attempt to process HEIC/HEIF files (upload as JPGs, no duplicate check). Requires SmugMug config setting or this flag.')
    parser.add_argument('--smugmug-album', help='(Optional) Target SmugMug album name. Overrides config file setting.')
    parser.add_argument('--smugmug-folder', help='(Optional) Target SmugMug folder path (e.g., "Folder/Subfolder"). Overrides config file setting.')
    parser.add_argument('--dry-run', action='store_true', help='Simulate transfer: perform checks but do not upload files.')
    parser.add_argument('--delete-from-google', action='store_true', help='[NOT IMPLEMENTED] Placeholder for future Google Photos deletion feature (currently only logs simulation).')
    parser.add_argument('--debug', action='store_true', help='Enable detailed debug logging to console and file.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--db-file', default=DB_FILE_DEFAULT, help='Path to the SQLite database file for tracking transfer state.')
    parser.add_argument('--force-refresh-list', action='store_true', help='Re-fetch the media list from Google Photos, clearing existing DB entries.')
    parser.add_argument('--retry-errors', action='store_true', help='[DEPRECATED - Errors are always retried] Include items currently marked with an error status in this processing run.')
    parser.add_argument('--reset-errors', action='store_true', help='Reset all items currently marked with an error status back to PENDING before starting the run.')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS, help=f'Number of parallel worker threads for processing items (default: {MAX_WORKERS}).')
    args = parser.parse_args()

    num_workers = args.workers
    if num_workers <= 0:
         print(f"Warning: Workers > 0. Using default: {MAX_WORKERS}", file=sys.stderr)
         num_workers = MAX_WORKERS

    setup_logging(args.debug)
    if not acquire_lock():
        sys.exit(1)

    # Setup signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, 'SIGBREAK'): # Windows specific
        signal.signal(signal.SIGBREAK, signal_handler)

    smugmug = None
    google_photos = None
    db_manager = None
    cleanup_handled_in_try = False
    start_time = time.time()
    processed_count_this_run = 0
    uploaded_in_run = 0
    duplicates_in_run = 0
    skipped_in_run = 0
    errors_in_run = 0
    total_db_items = 0 # Initialize total DB items count
    num_submitted = 0 # Initialize submitted count for the run
    quota_exceeded_flag.clear() # Ensure flag is clear at start

    # --- Initialize Cumulative Counters ---
    initial_uploaded_count = 0
    initial_duplicate_count = 0
    initial_skipped_count = 0
    initial_error_count = 0
    # --- End Initialize Cumulative Counters ---

    try:
        logger.info(f"--- Starting gp2sm v{__version__} ---")
        logger.info(f"Using database file: {args.db_file}")
        logger.info(f"Parallel Workers Enabled: Workers={num_workers}")
        logger.info(f"Command line arguments: {vars(args)}")
        logger.info("Initializing modules...")

        try:
             db_manager = DatabaseManager(db_file=args.db_file)
             db_manager_global = db_manager
             logger.info("DB manager initialized.")
             # --- Get TOTAL DB items count & Initial Stats EARLY ---
             total_db_items = db_manager.get_item_count()
             logger.info(f"Total items currently in database: {total_db_items}")
             initial_stats = db_manager.get_stats()
             if initial_stats:
                 initial_uploaded_count = initial_stats.get(STATUS_UPLOADED_SUCCESS, 0)
                 initial_duplicate_count = initial_stats.get(STATUS_DUPLICATE_HASH, 0) + \
                                           initial_stats.get(STATUS_DUPLICATE_FILENAME, 0)
                 initial_skipped_count = initial_stats.get(STATUS_SKIPPED_FILTER, 0) + \
                                         initial_stats.get(STATUS_SKIPPED_HEIC, 0)
                 # Include ALBUM_FULL and QUOTA in the initial error count for display consistency
                 initial_error_count = sum(initial_stats.get(s, 0) for s in ERROR_STATUSES) + \
                                       initial_stats.get(STATUS_ERROR_MISSING_DATA, 0) + \
                                       initial_stats.get(STATUS_ERROR_ALBUM_FULL, 0) + \
                                       initial_stats.get(STATUS_ERROR_QUOTA, 0)
                 logger.debug(f"Initial DB Stats: Up={initial_uploaded_count}, Dup={initial_duplicate_count}, Skip={initial_skipped_count}, Err={initial_error_count}")
             # --- End total count & initial stats fetch ---
        except Exception as e:
             logger.critical(f"Failed init DB manager: {e}", exc_info=True)
             print(f"Critical Error: Failed init DB: {e}", file=sys.stderr)
             sys.exit(1)

        # --- Reset Errored Items (Optional - BEFORE fetching list to process) ---
        if args.reset_errors:
            logger.info("Resetting errored items to PENDING...")
            reset_count = db_manager.reset_failed_items()
            logger.info(f"Reset {reset_count} items.")
            # Re-fetch total count and initial stats after reset
            total_db_items = db_manager.get_item_count()
            initial_stats = db_manager.get_stats()
            if initial_stats:
                 initial_uploaded_count = initial_stats.get(STATUS_UPLOADED_SUCCESS, 0)
                 initial_duplicate_count = initial_stats.get(STATUS_DUPLICATE_HASH, 0) + initial_stats.get(STATUS_DUPLICATE_FILENAME, 0)
                 initial_skipped_count = initial_stats.get(STATUS_SKIPPED_FILTER, 0) + initial_stats.get(STATUS_SKIPPED_HEIC, 0)
                 initial_error_count = sum(initial_stats.get(s, 0) for s in ERROR_STATUSES) + \
                                       initial_stats.get(STATUS_ERROR_MISSING_DATA, 0) + \
                                       initial_stats.get(STATUS_ERROR_ALBUM_FULL, 0) + \
                                       initial_stats.get(STATUS_ERROR_QUOTA, 0)
                 logger.debug(f"DB Stats after reset: Up={initial_uploaded_count}, Dup={initial_duplicate_count}, Skip={initial_skipped_count}, Err={initial_error_count}")

        # --- Initialize SmugMug ---
        try:
            smugmug = SmugMug(config_file='smugmug_config.json')
            smugmug.load_config()
        except FileNotFoundError:
             logger.warning(f"SM config '{smugmug.config_file}' missing.")
             print(f"\nWarning: SM config missing.", file=sys.stderr)
             if not smugmug.generate_default_config():
                 logger.critical("Failed generate default SM config.")
                 print("Critical Error: Failed to generate default SmugMug config.", file=sys.stderr)
                 sys.exit(1)
             else:
                 sys.exit(0) # Exit after generating default
        except Exception as e:
             logger.critical(f"Failed load SM config: {e}", exc_info=True)
             print(f"Critical Error: Failed load SM config: {e}", file=sys.stderr)
             sys.exit(1)

        # Apply overrides before auth
        current_smugmug_album_arg = args.smugmug_album
        current_smugmug_folder_arg = args.smugmug_folder
        current_google_album_arg = args.google_photos_album_id

        if args.process_heic:
            if smugmug.config:
                smugmug.config['process_heic'] = True
                logger.info("Override: HEIC processing enabled.")
            else:
                logger.error("Cannot apply HEIC override: SM config not loaded.")
                sys.exit(1)
        if current_smugmug_album_arg:
             logger.info(f"Override: SM Album: '{current_smugmug_album_arg}'")
             if smugmug.config:
                  smugmug.config['album_name'] = current_smugmug_album_arg
                  smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']
                  smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
             else:
                 logger.error("Cannot apply album override: SM config not loaded.")
                 sys.exit(1)
        if current_smugmug_folder_arg:
             logger.info(f"Override: SM Folder: '{current_smugmug_folder_arg}'")
             if smugmug.config:
                 smugmug.config['folder_name'] = current_smugmug_folder_arg
             else:
                 logger.error("Cannot apply folder override: SM config not loaded.")
                 sys.exit(1)
        elif 'folder_name' not in smugmug.config:
            smugmug.config['folder_name'] = None

        logger.info("Authenticating SmugMug...")
        if not smugmug.check_config_and_authenticate():
            logger.critical("SmugMug auth/config check failed.")
            sys.exit(1)
        logger.info("SmugMug init OK.")

        # --- DB Population / Config Check ---
        is_initial_run = False # Flag if DB was initially empty
        # total_db_items already fetched
        stored_config = db_manager.get_config_snapshot()

        initial_base_album_name = args.smugmug_album or smugmug.config.get('album_name')
        if not initial_base_album_name or initial_base_album_name == DEFAULT_SMUGMUG_CONFIG['album_name']:
             initial_base_album_name = (stored_config.get('initial_album_name') if stored_config and stored_config.get('initial_album_name') else smugmug.album_key) or 'UnknownAlbum'
        logger.debug(f"Initial base album name: '{initial_base_album_name}'")

        needs_fetch = (total_db_items == 0) or args.force_refresh_list
        if needs_fetch:
            is_initial_run = (total_db_items == 0)
            log_msg = "Force refresh..." if args.force_refresh_list else "DB empty, fetching..."
            logger.info(log_msg)
            try:
                logger.info("Initializing Google Photos...")
                # *** Pass quota_flag to constructor ***
                google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json', quota_flag=quota_exceeded_flag)
                google_photos_instance_global = google_photos
                if not google_photos.is_authenticated():
                     logger.critical("GP auth failed.")
                     print("Critical Error: GP auth failed.", file=sys.stderr)
                     sys.exit(1)
                logger.info("Google Photos init OK.")
            except GoogleCredentialsNotFoundError as e:
                print(f"\nError: {e}", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                logger.critical(f"Failed init GP module: {e}", exc_info=True)
                print(f"Critical Error: Failed init GP: {e}", file=sys.stderr)
                sys.exit(1)

            if not google_photos.refresh_token_if_needed():
                if not google_photos.authenticate():
                    logger.critical("GP re-auth failed.")
                    print("Critical Error: GP auth failed.", file=sys.stderr)
                    sys.exit(1)

            logger.info(f"Fetching media list from GP: {current_google_album_arg or 'Library'}...")
            photos_list = google_photos.get_photos(current_google_album_arg) # This now returns [] on quota error
            if quota_exceeded_flag.is_set(): # Check flag immediately after fetch
                 logger.critical("Google API Quota Exceeded during initial list fetch. Cannot proceed.")
                 print("\nCRITICAL: Google API Quota Exceeded. Please wait until quota resets (usually next day).", file=sys.stderr)
                 sys.exit(1)
            logger.info(f"Fetched {len(photos_list)} items from Google Photos.")

            if args.force_refresh_list and not is_initial_run:
                 logger.warning("Force refresh: Clearing DB...")
                 try:
                     with db_manager.conn:
                         db_manager.conn.execute(f"DELETE FROM {MEDIA_TABLE_NAME}")
                     logger.info("Cleared DB.")
                     total_db_items = 0 # Reset total count after clearing
                     # Reset initial stats as well
                     initial_uploaded_count = 0
                     initial_duplicate_count = 0
                     initial_skipped_count = 0
                     initial_error_count = 0
                 except Exception as del_e:
                     logger.error(f"Failed clear DB: {del_e}", exc_info=True)

            current_target_album_name = smugmug.album_name
            current_target_folder_path = smugmug.folder_name
            if current_target_album_name:
                 logger.info(f"Ensuring SM album '{current_target_album_name}' exists...")
                 if not smugmug.get_or_create_album_in_path(current_target_album_name, current_target_folder_path):
                      logger.critical("Failed find/create SM album.")
                      print(f"Critical Error: Failed find/create SM album '{current_target_album_name}'.", file=sys.stderr)
                      sys.exit(1)
            elif smugmug.album_key:
                logger.info(f"Using pre-configured SM key: {smugmug.album_key}")
            else:
                logger.critical("Config error: No SM album name or key.")
                sys.exit(1)
            current_target_album_key = smugmug.album_key
            current_target_album_uri = smugmug.album_api_uri
            logger.info(f"Confirmed Target - SM Key: {current_target_album_key}, URI: {current_target_album_uri}")

            if is_initial_run or args.force_refresh_list:
                if not db_manager.save_config_snapshot(initial_base_album_name, current_target_album_key, current_target_album_uri, smugmug.folder_name, current_google_album_arg):
                     logger.error("Failed save initial config snapshot.")

            if photos_list:
                 logger.info(f"Adding {len(photos_list)} items to DB...")
                 added_count = db_manager.add_item_batch(photos_list, current_target_album_key)
                 logger.info(f"Populated DB with {added_count} new items.")
                 total_db_items = db_manager.get_item_count() # Update total count after adding
            else:
                 logger.info("No items found in GP source.")
                 if is_initial_run:
                     logger.info("Exiting: no items found on initial run.")
                     sys.exit(0)

        else: # Resume run
             logger.info(f"DB contains {total_db_items} items. Checking config...")
             if stored_config:
                stored_sm_key = stored_config.get('current_album_key')
                stored_sm_folder = stored_config.get('current_folder_name')
                stored_gp_album = stored_config.get('google_album_id')
                stored_initial_album_name = stored_config.get('initial_album_name')
                current_folder_norm = smugmug.folder_name or None
                stored_folder_norm = stored_sm_folder or None
                current_gp_album_norm = current_google_album_arg or None
                stored_gp_album_norm = stored_gp_album or None

                # Determine effective key from args/config
                effective_current_key = None
                if args.smugmug_album:
                     temp_smugmug = SmugMug()
                     temp_smugmug.config = smugmug.config.copy()
                     temp_smugmug.auth_session = smugmug.auth_session
                     temp_smugmug.user_uri = smugmug.user_uri
                     if temp_smugmug.get_or_create_album_in_path(args.smugmug_album, current_folder_norm):
                         effective_current_key = temp_smugmug.album_key
                     del temp_smugmug
                elif smugmug.album_name:
                     temp_smugmug = SmugMug()
                     temp_smugmug.config = smugmug.config.copy()
                     temp_smugmug.auth_session = smugmug.auth_session
                     temp_smugmug.user_uri = smugmug.user_uri
                     if temp_smugmug.get_or_create_album_in_path(smugmug.album_name, current_folder_norm):
                         effective_current_key = temp_smugmug.album_key
                     del temp_smugmug
                else:
                    effective_current_key = smugmug.album_key

                mismatch = (effective_current_key and stored_sm_key and effective_current_key != stored_sm_key) or \
                           (current_folder_norm != stored_folder_norm) or \
                           (current_gp_album_norm != stored_gp_album_norm)

                if mismatch:
                    warning_lines = [
                        "="*60,
                        "WARNING: CONFIG MISMATCH!",
                        ">>> CONTINUING WITH STORED DB SETTINGS <<<",
                        f"    SM Key: {stored_sm_key}",
                        f"    SM Folder: {stored_folder_norm or 'Root'}",
                        f"    GP Album: {stored_gp_album_norm or 'Library'}",
                        "\nUse --force-refresh-list or delete DB to use NEW settings.",
                        "="*60
                    ]
                    for line in warning_lines:
                        logger.warning(line)
                    time.sleep(5)
                    # Apply stored settings
                    smugmug.album_key = stored_sm_key
                    smugmug.album_api_uri = stored_config.get('current_album_uri')
                    smugmug.folder_name = stored_folder_norm
                    smugmug.album_name = stored_initial_album_name # Use stored initial name
                    initial_base_album_name = stored_initial_album_name or initial_base_album_name
                else:
                    logger.info("Config matches DB snapshot.")
                    # Ensure SM object uses stored config for consistency
                    smugmug.album_key = stored_config.get('current_album_key')
                    smugmug.album_api_uri = stored_config.get('current_album_uri')
                    smugmug.folder_name = stored_config.get('current_folder_name')
                    smugmug.album_name = stored_config.get('initial_album_name')
                    initial_base_album_name = stored_config.get('initial_album_name') or initial_base_album_name
             else: # Existing DB, no snapshot
                logger.warning("Existing DB found, but no config snapshot. Saving current config...")
                current_target_album_name = smugmug.album_name
                current_target_folder_path = smugmug.folder_name
                if current_target_album_name:
                     if not smugmug.get_or_create_album_in_path(current_target_album_name, current_target_folder_path):
                          logger.critical("Failed find/create target SM album.")
                          print(f"Critical Error: Failed find/create SM album '{current_target_album_name}'.", file=sys.stderr)
                          sys.exit(1)
                elif not smugmug.album_key:
                    logger.critical("Config error: No SM album name or key.")
                    sys.exit(1)
                if not db_manager.save_config_snapshot(initial_base_album_name, smugmug.album_key, smugmug.album_api_uri, smugmug.folder_name, current_google_album_arg):
                     logger.error("Failed save current config snapshot.")

        # --- Initialize Google Photos (if not done already) ---
        if google_photos is None:
            try:
                logger.info("Initializing Google Photos...")
                # *** Pass quota_flag to constructor ***
                google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json', quota_flag=quota_exceeded_flag)
                google_photos_instance_global = google_photos
                if not google_photos.is_authenticated():
                     logger.critical("GP auth failed.")
                     print("Critical Error: GP auth failed.", file=sys.stderr)
                     sys.exit(1)
                logger.info("Google Photos init OK.")
            except GoogleCredentialsNotFoundError as e:
                print(f"\nError: {e}", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                logger.critical(f"Failed init GP module: {e}", exc_info=True)
                print(f"Critical Error: Failed init GP: {e}", file=sys.stderr)
                sys.exit(1)

        # --- Get Items for Processing ---
        logger.info("Fetching items to process from database...")
        items_to_process_list = db_manager.get_items_to_process()
        num_submitted = len(items_to_process_list) # Number of items for THIS run
        logger.info(f"Found {num_submitted} items requiring processing (out of {total_db_items} total).")

        if num_submitted == 0:
             logger.info("No items require processing.")
        else:
            logger.info(f"Starting {num_workers} workers to process {num_submitted} items...")
            # --- Parallel Processing ---
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='Worker') as executor:
                submitted_futures = []
                logger.info(f"Submitting {num_submitted} items...")
                for item_details in items_to_process_list:
                    if shutdown_event.is_set():
                        logger.warning("Shutdown during submission.")
                        break
                    future = executor.submit(process_item_worker, item_details, google_photos, smugmug, db_manager, args)
                    submitted_futures.append(future)

                logger.info(f"Submitted {len(submitted_futures)} tasks. Waiting...")
                processed_count_this_run = 0 # Counter for items completed in this run
                active_futures = submitted_futures

                # Calculate items already completed before this run
                items_completed_before_run = total_db_items - num_submitted

                for future in concurrent.futures.as_completed(active_futures):
                    processed_count_this_run += 1
                    try:
                        google_id, final_status = future.result()

                        # Update run-specific summary counters
                        if final_status == STATUS_UPLOADED_SUCCESS:
                            uploaded_in_run += 1
                        elif final_status in [STATUS_DUPLICATE_FILENAME, STATUS_DUPLICATE_HASH]:
                            duplicates_in_run += 1
                        elif final_status in [STATUS_SKIPPED_FILTER, STATUS_SKIPPED_HEIC]:
                            skipped_in_run += 1
                        # Count QUOTA as error for run summary
                        elif final_status in ERROR_STATUSES or final_status == STATUS_ERROR_ALBUM_FULL or final_status == STATUS_ERROR_QUOTA:
                            errors_in_run += 1

                        # --- Calculate and Log Overall Progress & Stats ---
                        overall_completed_count = items_completed_before_run + processed_count_this_run
                        percentage = (overall_completed_count / total_db_items) * 100 if total_db_items > 0 else 0
                        last_id_short = f"{google_id[:8]}..." if google_id else "N/A"

                        # Calculate cumulative totals for logging
                        current_total_uploaded = initial_uploaded_count + uploaded_in_run
                        current_total_duplicates = initial_duplicate_count + duplicates_in_run
                        current_total_skipped = initial_skipped_count + skipped_in_run
                        # Add run errors to initial errors for cumulative display
                        current_total_errors = initial_error_count + errors_in_run

                        logger.progress(
                            f"Progress: {overall_completed_count}/{total_db_items} ({percentage:.1f}%) "
                            f"| Totals: Up={current_total_uploaded} Dup={current_total_duplicates} Skip={current_total_skipped} Err={current_total_errors} " # Show overall stats
                            f"| Last: {final_status} (ID: {last_id_short})"
                        )
                        # --- End Overall Progress Logging ---

                    except Exception as exc:
                        logger.error(f"Exception retrieving worker result: {exc}", exc_info=True)
                        errors_in_run += 1 # Count as error for run summary
                        # Log overall progress even on error
                        overall_completed_count = items_completed_before_run + processed_count_this_run
                        percentage = (overall_completed_count / total_db_items) * 100 if total_db_items > 0 else 0
                        # Calculate cumulative totals for logging on error
                        current_total_uploaded = initial_uploaded_count + uploaded_in_run
                        current_total_duplicates = initial_duplicate_count + duplicates_in_run
                        current_total_skipped = initial_skipped_count + skipped_in_run
                        current_total_errors = initial_error_count + errors_in_run
                        logger.error(
                            f"Progress: {overall_completed_count}/{total_db_items} ({percentage:.1f}%) "
                            f"| Totals: Up={current_total_uploaded} Dup={current_total_duplicates} Skip={current_total_skipped} Err={current_total_errors} "
                            f"| Last: ERROR retrieving future result"
                        )


                    if shutdown_event.is_set():
                        logger.warning("Shutdown requested. Breaking from processing results.")
                        break

                logger.info("Worker processing loop finished or interrupted.")

        # --- Final Summary ---
        total_duration = time.time() - start_time
        logger.info("=" * 60)
        logger.info("Run Summary (This Execution):") # Clarify summary scope
        logger.info(f"  Items Submitted This Run:      {num_submitted}")
        logger.info(f"  Items Processed (Completed):   {processed_count_this_run}")
        logger.info(f"  Uploaded this Run:           {uploaded_in_run}")
        logger.info(f"  Marked as Duplicate this Run: {duplicates_in_run}")
        logger.info(f"  Skipped this Run:            {skipped_in_run}")
        logger.info(f"  Errors this Run:             {errors_in_run}")
        logger.info(f"  Total processing time:       {total_duration:.2f} seconds")
        logger.info("-" * 60)
        logger.info("Overall Database Stats (Cumulative):") # Clarify summary scope
        final_stats = db_manager.get_stats() if db_manager else {}
        final_total_db_items = sum(final_stats.values())
        logger.info(f"  Total Items in DB:           {final_total_db_items}")
        for status, count in sorted(final_stats.items()):
            logger.info(f"  - {status}: {count}")
        logger.info("=" * 60)

        logger.info("--- Concise Run Summary ---")
        logger.info(f"- Items Submitted: {num_submitted}")
        logger.info(f"- Items Processed: {processed_count_this_run}")
        logger.info(f"- Uploaded{' (Dry Run)' if args.dry_run else ''}:      {uploaded_in_run}")
        logger.info(f"- Duplicates Found: {duplicates_in_run}")
        logger.info(f"- Skipped:          {skipped_in_run}")
        logger.info(f"- Errors:           {errors_in_run}")

        final_db_filename = args.db_file
        # Check for persistent errors (excluding album full and quota, as they indicate rerun needed)
        final_error_count_for_exit = sum(final_stats.get(s, 0) for s in ERROR_STATUSES if s not in [STATUS_ERROR_ALBUM_FULL, STATUS_ERROR_QUOTA]) + \
                                     final_stats.get(STATUS_ERROR_MISSING_DATA, 0)
        album_switch_occurred_this_run = final_stats.get(STATUS_ERROR_ALBUM_FULL, 0) > 0
        quota_error_occurred_this_run = final_stats.get(STATUS_ERROR_QUOTA, 0) > 0 or quota_exceeded_flag.is_set()

        run_completed_successfully = (final_error_count_for_exit == 0 and not shutdown_requested)

        if quota_error_occurred_this_run:
             logger.critical("- Status: STOPPED DUE TO GOOGLE API QUOTA LIMIT.")
             logger.critical(">>> Please wait until your quota resets (usually next day) and run again. <<<")
        elif run_completed_successfully and not album_switch_occurred_this_run:
             logger.info("- Status: Completed run without persistent errors or album switches")
             remaining_items_count = final_total_db_items - sum(final_stats.get(s, 0) for s in TERMINAL_STATUSES)
             if remaining_items_count == 0:
                 logger.info("All items processed successfully. Run complete.")
                 logger.info("Attempting to rename completed database file...")
                 if db_manager_global:
                     db_manager_global.close()
                     logger.info("Closed DB before rename.")
                     db_manager_global = None
                 else:
                     logger.warning("DB manager not found for closing before rename.")
                 base_db_name, db_ext = os.path.splitext(args.db_file)
                 timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                 new_db_filename = f"{base_db_name}_completed_{timestamp}{db_ext}"
                 try:
                     if os.path.exists(args.db_file):
                         os.rename(args.db_file, new_db_filename)
                         logger.info(f"Successfully renamed database to: {new_db_filename}")
                         final_db_filename = new_db_filename
                         cleanup_handled_in_try = True
                         release_lock()
                     else:
                         logger.warning(f"DB file {args.db_file} not found for renaming.")
                 except OSError as e:
                     logger.error(f"Failed rename DB: {e}", exc_info=True)
                     print(f"\nERROR: Failed rename DB: {e}", file=sys.stderr)
             else:
                 logger.warning(f"Run completed without persistent errors, but {remaining_items_count} items remain. DB not renamed.")
        elif album_switch_occurred_this_run:
             album_full_count = final_stats.get(STATUS_ERROR_ALBUM_FULL, 0)
             logger.warning(f"- Status: Album switched during run. {album_full_count} items marked 'ERROR_ALBUM_FULL'.")
             logger.warning(">>> Please re-run the script to continue processing with the new album. <<<")
        elif shutdown_requested:
            logger.warning("- Status: Terminated by user")
        else:
            logger.error(f"- Status: Completed run with {final_error_count_for_exit} persistent errors")

        logger.info(f"- Run Time: {total_duration:.2f} sec")
        logger.info("-" * 60)
        logger.info("Overall Database Stats (Final):")
        for status, count in sorted(final_stats.items()):
             if count > 0:
                 logger.info(f"- {status}: {count}")
        logger.info(f"- Detailed Log File: {LOG_FILE}")
        logger.info(f"- Database File: {final_db_filename}")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        if not shutdown_requested:
             log_func = getattr(logger, 'warning', print)
             log_func("Keyboard interrupt. Shutting down...")
             shutdown_event.set()
             shutdown_requested = True
             quota_exceeded_flag.set() # Treat interrupt like quota for exit msg
    except Exception as e:
        log_func = getattr(logger, 'critical', lambda msg, exc_info: print(msg, file=sys.stderr))
        log_func(f"Critical unexpected error: {e}", exc_info=True)
        print(f"\nCritical Error: {e}. Check log '{LOG_FILE}'.", file=sys.stderr)
        shutdown_event.set()
        shutdown_requested = True
        quota_exceeded_flag.set() # Treat critical like quota for exit msg

    finally:
        log_func_info = getattr(logger, 'info', print)
        log_func_info("Executing final cleanup...")
        shutdown_event.set()
        if not cleanup_handled_in_try:
            cleanup(google_photos_instance_global, db_manager_global)
        else:
            log_func_info("Skipping normal cleanup as DB rename handled it.")

        # Adjust exit code: Exit 0 only if run completed successfully AND no album switch/quota error happened
        final_error_count_for_exit = sum(final_stats.get(s, 0) for s in ERROR_STATUSES if s not in [STATUS_ERROR_ALBUM_FULL, STATUS_ERROR_QUOTA]) + \
                                     final_stats.get(STATUS_ERROR_MISSING_DATA, 0) if 'final_stats' in locals() else 1
        album_switch_occurred_final = final_stats.get(STATUS_ERROR_ALBUM_FULL, 0) > 0 if 'final_stats' in locals() else False
        quota_error_occurred_final = final_stats.get(STATUS_ERROR_QUOTA, 0) > 0 or quota_exceeded_flag.is_set() if 'final_stats' in locals() else quota_exceeded_flag.is_set()
        exit_code = 0
        if final_error_count_for_exit > 0 or shutdown_requested or album_switch_occurred_final or quota_error_occurred_final:
            exit_code = 1

        log_func_info(f"Exiting script with code {exit_code}.")
        logging.shutdown()
        sys.exit(exit_code)

# --- Script Entry Point ---
if __name__ == "__main__":
    main()
