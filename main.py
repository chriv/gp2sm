# Google Photos to SmugMug Transfer Script (v2.0)
# Implements parallel pipeline processing: Download -> Hash/Check -> Upload
# Corrected indentation of main try/except/finally block.

__version__ = "2.0"

# Standard library imports
import argparse
import logging
import os
import sys
import time
import json
import datetime
from logging.handlers import RotatingFileHandler
import signal
import queue
import concurrent.futures
import threading
import traceback # For detailed error logging in threads

# Third-party imports
import colorlog

# Local module imports
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug, DEFAULT_SMUGMUG_CONFIG
from database_manager import (
    DatabaseManager, DB_FILE_DEFAULT, MEDIA_TABLE_NAME,
    STATUS_PENDING, STATUS_HASHED, STATUS_SMUGMUG_CHECKED_NOT_FOUND,
    STATUS_DOWNLOADED_FOR_UPLOAD, STATUS_UPLOAD_ATTEMPTED, STATUS_UPLOADED_SUCCESS,
    STATUS_DUPLICATE_HASH, STATUS_DUPLICATE_FILENAME, STATUS_SKIPPED_FILTER,
    STATUS_SKIPPED_HEIC, STATUS_ERROR_DOWNLOAD, STATUS_ERROR_HASHING,
    STATUS_ERROR_SMUGMUG_API, STATUS_ERROR_UPLOAD_FAILED, STATUS_ERROR_UNKNOWN,
    STATUS_ERROR_MISSING_DATA, TERMINAL_STATUSES, ERROR_STATUSES
)


# --- Constants ---
LOG_FILE = "gp2sm_transfer.log"
LOCK_FILE = "gp2sm.lock"
LOG_ID_TRUNCATE_LEN = 8
# --- Concurrency Settings ---
MAX_DOWNLOAD_WORKERS = 5 # Number of concurrent downloads
MAX_PROCESSING_WORKERS = 3 # Number of concurrent hash/check workers
MAX_UPLOAD_WORKERS = 1 # Number of concurrent uploads (usually 1)
# Buffer between download and processing stages
DOWNLOAD_COMPLETE_BUFFER = 5
# Buffer between processing and upload stages (can be larger)
UPLOAD_QUEUE_BUFFER = 10

# --- Global Variables ---
logger = None
shutdown_requested = False
google_photos_instance_global = None
db_manager_global = None
# Use events for signaling shutdown across threads
shutdown_event = threading.Event()

# --- Signal Handling ---
def signal_handler(sig, frame):
    global shutdown_requested, logger, shutdown_event
    if not shutdown_requested:
        signal_name = f"Signal {sig}"
        try:
            signal_name = signal.Signals(sig).name
        except ValueError:
            pass # Keep default signal name if not found
        log_func = getattr(logger, 'warning', print)
        print_func = print
        log_func(f"Received signal {signal_name}. Initiating graceful shutdown...")
        print_func(f"\n>>> Signal {signal_name} received. Stopping submission of new tasks... <<<")
        shutdown_requested = True
        shutdown_event.set() # Signal threads to stop
    else:
        if logger:
            logger.debug(f"Shutdown already in progress. Received signal {sig} again.")
        print(">>> Shutdown already requested. Please wait. <<<")


# --- Logging Setup ---
def setup_logging(debug=False):
    global logger
    log_level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(log_level)

    debug_format = ('%(asctime)s - %(log_color)s%(levelname)-8s%(reset)s - '
                    '[%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - '
                    '%(message_log_color)s%(message)s%(reset)s')
    info_format = ('%(asctime)s - %(log_color)s%(levelname)-8s%(reset)s - '
                   '[%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - ' # Keep threadname for info too
                   '%(message_log_color)s%(message)s%(reset)s')
    # Use debug format if debug is True, otherwise use info format
    log_format = debug_format if debug else info_format

    console_formatter = colorlog.ColoredFormatter(
        log_format, datefmt='%Y-%m-%d %H:%M:%S', reset=True,
        log_colors={'DEBUG':'cyan','INFO':'green','WARNING':'yellow','ERROR':'red','CRITICAL':'red,bg_white'},
        secondary_log_colors={'message': {'ERROR':'red','CRITICAL':'red','WARNING':'yellow'}}, style='%'
    )
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addHandler(console_handler)

    file_format = '%(asctime)s - %(levelname)-8s - [%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - %(message)s'
    file_formatter = logging.Formatter(file_format, datefmt='%Y-%m-%d %H:%M:%S')
    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"Warning: Could not configure file logging to '{LOG_FILE}': {e}", file=sys.stderr)
        if logger:
            logger.error(f"Failed to set up file logging handler: {e}", exc_info=True)

    # Silence verbose libraries
    for lib_logger_name in ["googleapiclient.discovery_cache", "google.auth.transport.requests",
                            "urllib3.connectionpool", "requests_oauthlib.oauth1_session"]:
        logging.getLogger(lib_logger_name).setLevel(logging.WARNING)
    logging.getLogger("database_manager").setLevel(log_level)


# --- Lock File Management ---
def acquire_lock():
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        if logger:
            logger.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        log_msg = f"Lock file '{LOCK_FILE}' already exists. Another instance may be running."
        if logger:
            logger.error(log_msg)
        print(f"Error: {log_msg}", file=sys.stderr)
        print("If sure no other instance is running, delete the lock file.", file=sys.stderr)
        return False
    except OSError as e:
        log_msg = f"Error acquiring lock file '{LOCK_FILE}': {e}"
        if logger:
            logger.error(log_msg, exc_info=True)
        print(f"Error: {log_msg}", file=sys.stderr)
        return False

def release_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            if logger:
                logger.info(f"Released lock file: {LOCK_FILE}")
        except OSError as e:
            if logger:
                logger.warning(f"Could not remove lock file '{LOCK_FILE}': {e}", exc_info=True)
            print(f"Warning: Could not remove lock file '{LOCK_FILE}': {e}", file=sys.stderr)


# --- Cleanup Function ---
def cleanup(google_photos_instance, db_manager_instance):
    log_func_info = getattr(logger, 'info', lambda msg: print(f"INFO: {msg}"))
    log_func_debug = getattr(logger, 'debug', lambda msg: print(f"DEBUG: {msg}"))
    log_func_error = getattr(logger, 'error', lambda msg: print(f"ERROR: {msg}"))

    log_func_info("--- Running cleanup procedures ---")
    if db_manager_instance and hasattr(db_manager_instance, 'close') and callable(db_manager_instance.close):
         try:
             log_func_debug("Closing database connection...")
             db_manager_instance.close()
         except Exception as e:
             log_func_error(f"Error closing database connection: {e}", exc_info=True)
    else:
         log_func_debug("No database manager instance provided or close method missing.")

    if google_photos_instance and hasattr(google_photos_instance, 'cleanup_temp_dir') and callable(google_photos_instance.cleanup_temp_dir):
        try:
            log_func_debug("Calling Google Photos temporary directory cleanup...")
            google_photos_instance.cleanup_temp_dir()
        except Exception as e:
            log_func_error(f"Error during Google Photos temp directory cleanup: {e}", exc_info=True)
    else:
        log_func_debug("No Google Photos instance provided or cleanup method missing.")

    release_lock()
    log_func_info("--- Cleanup complete ---")


# --- Worker Functions ---

def download_stage_worker(item_details, google_photos, db_manager, download_complete_queue, upload_queue):
    """
    Downloads the item if necessary (for hashing or direct upload).
    Puts (item_details, temp_file_path, refreshed_details) onto download_complete_queue on success.
    If download is not needed (e.g., video already checked by filename), puts item directly onto upload_queue.
    Updates DB on failure.
    """
    google_id = item_details['google_id']
    filename = item_details['filename']
    mime_type = item_details['mime_type']
    is_video = mime_type.startswith('video/')
    is_heic = filename.lower().endswith('.heic')
    current_md5_hash = item_details['md5_hash']
    should_process_heic = item_details.get('_should_process_heic', False) # Get flag passed down

    # Determine if download is needed *at this stage*
    # Needed if: Image without hash OR Video OR HEIC to be processed
    needs_download = (not is_video and not is_heic and not current_md5_hash) or is_video or (is_heic and should_process_heic)

    if not needs_download:
         logger.debug(f"Download not needed for {google_id} ('{filename}'). Passing to next stage.")
         # Put None for temp_file_path, indicating no download occurred here
         download_complete_queue.put((item_details, None, None))
         return

    # Proceed with download
    logger.debug(f"Download worker started for {google_id} ('{filename}')")
    try:
        result_tuple = google_photos.download_photo(item_details)
        if result_tuple and result_tuple[0]:
            temp_file_path, _, _, refreshed_details = result_tuple
            logger.info(f"Download successful for {google_id} ('{filename}'). Path: {temp_file_path}")
            download_complete_queue.put((item_details, temp_file_path, refreshed_details))
        else:
            logger.error(f"Download worker failed for {google_id} ('{filename}').")
            db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Download function returned failure")
    except Exception as e:
        logger.error(f"Exception in download worker for {google_id} ('{filename}'): {e}\n{traceback.format_exc()}")
        db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, f"Worker exception: {e}")


def processing_stage_worker(item_details, temp_file_path, refreshed_details, smugmug, db_manager, upload_queue):
    """
    Handles Hashing and SmugMug Check stages.
    Takes input from download_complete_queue.
    Puts (item_details, temp_file_path) onto upload_queue if not duplicate/error.
    Updates DB status appropriately. Cleans up temp_file if processing stops here.
    """
    google_id = item_details['google_id']
    filename = item_details['filename']
    mime_type = item_details['mime_type']
    current_md5_hash = item_details['md5_hash'] # Hash from DB
    is_video = mime_type.startswith('video/')
    is_heic = filename.lower().endswith('.heic')
    should_process_heic = item_details.get('_should_process_heic', False) # Get flag

    truncated_id = f"{google_id[:LOG_ID_TRUNCATE_LEN]}...{google_id[-LOG_ID_TRUNCATE_LEN:]}" if len(google_id) > LOG_ID_TRUNCATE_LEN * 2 else google_id
    log_identifier = f"Processing Stage (ID: {truncated_id}, File: '{filename}')"
    logger.debug(f"{log_identifier}: Worker started.")

    try:
        # --- Handle Refreshed Details from Download Stage ---
        if refreshed_details:
            logger.debug(f"{log_identifier}: Details were refreshed during download. Updating DB.")
            new_base_url = refreshed_details.get('baseUrl')
            new_metadata_json = json.dumps(refreshed_details.get('mediaMetadata', {}))
            db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
            # Update item_details dict in memory in case needed later
            item_details['baseUrl'] = new_base_url
            item_details['media_metadata_json'] = new_metadata_json

        # --- Hashing (if needed) ---
        if not is_video and not is_heic and not current_md5_hash:
            if not temp_file_path or not os.path.exists(temp_file_path):
                 logger.error(f"{log_identifier}: Temp file missing for hashing. Cannot proceed.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Temp file missing for hash")
                 return # Stop processing this item

            logger.info(f"{log_identifier}: Calculating MD5 hash...")
            calculated_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
            if not calculated_hash:
                logger.error(f"{log_identifier}: MD5 hash calculation failed.")
                db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, "MD5 calculation failed")
                # Cleanup temp file
                if os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except OSError:
                        pass # Ignore error during cleanup
                return
            else:
                logger.debug(f"{log_identifier}: Calculated MD5: {calculated_hash}. Updating DB.")
                current_md5_hash = calculated_hash
                db_manager.update_item_status(google_id, STATUS_HASHED, md5_hash=current_md5_hash)

        # --- SmugMug Check (if not HEIC) ---
        if is_heic and should_process_heic:
            logger.debug(f"{log_identifier}: Skipping SM check (HEIC).")
            # Proceed directly to upload queue if download happened
            if temp_file_path:
                 upload_queue.put((item_details, temp_file_path))
            else:
                 logger.error(f"{log_identifier}: HEIC processing requested but no temp file path. Aborting item.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_UNKNOWN, "HEIC processing inconsistency")
            return

        # Proceed with check for non-HEIC items
        exists_on_smugmug = False
        log_reason = ""
        target_album_key_for_check = smugmug.album_key # Use the key set on the SmugMug object
        if not target_album_key_for_check:
             logger.error(f"{log_identifier}: SmugMug target album key missing. Cannot check.")
             db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, "SM album key missing")
             # Cleanup temp file if it exists
             if temp_file_path and os.path.exists(temp_file_path):
                 try:
                     os.remove(temp_file_path)
                 except OSError:
                     pass # Ignore error during cleanup
             return

        logger.info(f"{log_identifier}: Checking SmugMug for duplicates...")
        if is_video:
             log_reason = "filename match"
             exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type)
        elif not is_video: # Standard image
             log_reason = "MD5 hash match"
             if current_md5_hash:
                  exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type, file_hash=current_md5_hash)
             else:
                  logger.error(f"{log_identifier}: Cannot check SmugMug, MD5 hash missing.")
                  db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, "MD5 missing for SM check")
                  if temp_file_path and os.path.exists(temp_file_path):
                      try:
                          os.remove(temp_file_path)
                      except OSError:
                          pass # Ignore error during cleanup
                  return

        # --- Handle Check Result ---
        if exists_on_smugmug:
             duplicate_status = STATUS_DUPLICATE_FILENAME if is_video else STATUS_DUPLICATE_HASH
             logger.info(f"{log_identifier}: Found on SmugMug ({log_reason}). Marking duplicate.")
             db_manager.update_item_status(google_id, duplicate_status, error_message=f"Duplicate check via {log_reason}")
             # Cleanup temp file if it exists
             if temp_file_path and os.path.exists(temp_file_path):
                  try:
                      os.remove(temp_file_path)
                      logger.debug(f"{log_identifier}: Cleaned up temp file for duplicate.")
                  except OSError as e:
                      logger.warning(f"{log_identifier}: Failed to clean up temp file for duplicate: {e}")
             # No need to put on upload queue
        else:
             logger.info(f"{log_identifier}: Checked SmugMug via {log_reason}: Not found.")
             db_manager.update_item_status(google_id, STATUS_SMUGMUG_CHECKED_NOT_FOUND, error_message=f"SM check via {log_reason} - not found")
             # Put item onto upload queue (pass original details and path)
             if temp_file_path: # Only queue for upload if we have a file
                  logger.debug(f"{log_identifier}: Queueing for upload.")
                  upload_queue.put((item_details, temp_file_path))
             else:
                  logger.error(f"{log_identifier}: Item not found on SmugMug, but no temp file path available to queue for upload. Skipping upload.")
                  db_manager.update_item_status(google_id, STATUS_ERROR_UNKNOWN, "File path missing before upload queue")


    except Exception as e:
        logger.error(f"Exception in processing worker for {google_id} ('{filename}'): {e}\n{traceback.format_exc()}")
        db_manager.update_item_status(google_id, STATUS_ERROR_UNKNOWN, f"Processing worker exception: {e}")
        # Ensure cleanup if error occurred mid-processing
        if temp_file_path and os.path.exists(temp_file_path):
             try:
                 os.remove(temp_file_path)
             except OSError:
                 pass # Ignore error during cleanup


def upload_stage_worker(item_details, temp_file_path, smugmug, db_manager, google_photos, delete_from_google, dry_run):
    """
    Handles the SmugMug Upload stage.
    Takes input from upload_queue.
    Updates DB status. Cleans up temp file via smugmug.upload_media.
    """
    google_id = item_details['google_id']
    filename = item_details['filename']
    mime_type = item_details['mime_type']

    truncated_id = f"{google_id[:LOG_ID_TRUNCATE_LEN]}...{google_id[-LOG_ID_TRUNCATE_LEN:]}" if len(google_id) > LOG_ID_TRUNCATE_LEN * 2 else google_id
    log_identifier = f"Upload Stage (ID: {truncated_id}, File: '{filename}')"
    logger.debug(f"{log_identifier}: Worker started.")

    try:
        if dry_run:
            logger.info(f"{log_identifier}: [DRY RUN] Would upload.")
            # Simulate success for dry run, don't actually change DB status beyond checks
            # But DO simulate deletion if requested
            if delete_from_google:
                 google_photos.remove_photo(google_id, dry_run=True)
            # Manually clean up file in dry run as upload_media won't be called
            if temp_file_path and os.path.exists(temp_file_path):
                 try:
                     os.remove(temp_file_path)
                     logger.debug(f"{log_identifier}: [DRY RUN] Cleaned temp file.")
                 except OSError as e:
                     logger.warning(f"{log_identifier}: [DRY RUN] Failed to clean temp file: {e}")
            return # End worker for dry run

        # --- Perform Upload ---
        target_album_uri_for_upload = smugmug.album_api_uri
        if not target_album_uri_for_upload:
             logger.error(f"{log_identifier}: SmugMug target album URI missing. Cannot upload.")
             db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, "SM album URI missing")
             # Cleanup temp file
             if temp_file_path and os.path.exists(temp_file_path):
                 try:
                     os.remove(temp_file_path)
                 except OSError:
                     pass # Ignore error during cleanup
             return

        if not temp_file_path or not os.path.exists(temp_file_path):
             logger.error(f"{log_identifier}: Temp file missing before upload. Cannot upload.")
             db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Temp file missing for upload")
             return

        logger.info(f"{log_identifier}: Uploading to SmugMug URI: {target_album_uri_for_upload}...")
        db_manager.update_item_status(google_id, STATUS_UPLOAD_ATTEMPTED, increment_attempt=True)

        upload_success = smugmug.upload_media(target_album_uri_for_upload, temp_file_path, filename, mime_type)
        # upload_media handles cleaning up temp_file_path

        if upload_success:
            logger.info(f"{log_identifier}: Upload successful.")
            db_manager.update_item_status(google_id, STATUS_UPLOADED_SUCCESS)
            if delete_from_google:
                 google_photos.remove_photo(google_id, dry_run=False) # Actual (simulated) delete
        else:
            logger.error(f"{log_identifier}: Upload failed.")
            db_manager.update_item_status(google_id, STATUS_ERROR_UPLOAD_FAILED, "Upload function returned failure")

    except Exception as e:
        logger.error(f"Exception in upload worker for {google_id} ('{filename}'): {e}\n{traceback.format_exc()}")
        db_manager.update_item_status(google_id, STATUS_ERROR_UPLOAD_FAILED, f"Upload worker exception: {e}")
        # Ensure cleanup if error occurred before upload_media call
        if temp_file_path and os.path.exists(temp_file_path):
             try:
                 os.remove(temp_file_path)
             except OSError:
                 pass # Ignore error during cleanup


# --- Main Function ---
def main():
    global logger, shutdown_requested, google_photos_instance_global, db_manager_global, shutdown_event
    parser = argparse.ArgumentParser(
        description="Transfer Google Photos to SmugMug using SQLite and parallel pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # Argument parsing (add concurrency args later if needed)
    parser.add_argument('--google-photos-album-id', help='(Optional) Google Photos Album ID (for initial DB population).')
    parser.add_argument('--ignore-photos', action='store_true', help='Skip processing photos.')
    parser.add_argument('--ignore-videos', action='store_true', help='Skip processing videos.')
    parser.add_argument('--process-heic', action='store_true', help='Process HEIC files (upload as JPGs, no duplicate check).')
    parser.add_argument('--smugmug-album', help='(Optional) Target SmugMug album name (overrides config).')
    parser.add_argument('--smugmug-folder', help='(Optional) Target SmugMug folder path (overrides config).')
    parser.add_argument('--dry-run', action='store_true', help='Simulate transfer without uploading.')
    parser.add_argument('--delete-from-google', action='store_true', help='(Simulated) Log deletion from Google Photos after success.')
    parser.add_argument('--debug', action='store_true', help='Enable detailed debug logging.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--db-file', default=DB_FILE_DEFAULT, help='Path to the SQLite database file.')
    parser.add_argument('--force-refresh-list', action='store_true', help='Re-fetch list from Google Photos.')
    parser.add_argument('--retry-errors', action='store_true', help='Re-process items marked with error status.')
    parser.add_argument('--reset-errors', action='store_true', help='Reset error items to PENDING before start.')

    args = parser.parse_args()

    setup_logging(args.debug)

    if not acquire_lock():
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal_handler)

    smugmug = None
    google_photos = None
    db_manager = None
    cleanup_handled_in_try = False # Initialize flag here
    start_time = time.time()
    # Initialize run summary counters here
    processed_in_run = 0; uploaded_in_run = 0; duplicates_in_run = 0; skipped_in_run = 0; errors_in_run = 0
    total_items_to_process = 0 # Will be updated after fetching list

    # --- Initialize Queues ---
    download_complete_queue = queue.Queue(maxsize=DOWNLOAD_COMPLETE_BUFFER)
    upload_queue = queue.Queue(maxsize=UPLOAD_QUEUE_BUFFER)

    # --- Start Main Try/Except/Finally Block ---
    try:
        # --- Initialize ThreadPoolExecutors using 'with' ---
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS, thread_name_prefix='Downloader') as download_executor, \
             concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PROCESSING_WORKERS, thread_name_prefix='Processor') as processing_executor, \
             concurrent.futures.ThreadPoolExecutor(max_workers=MAX_UPLOAD_WORKERS, thread_name_prefix='Uploader') as upload_executor:

            # --- Code previously inside the main try block goes here ---
            logger.info(f"--- Starting gp2sm v{__version__} ---")
            logger.info(f"Using database file: {args.db_file}")
            logger.info(f"Parallel Pipeline Enabled: Download={MAX_DOWNLOAD_WORKERS}, Process={MAX_PROCESSING_WORKERS}, Upload={MAX_UPLOAD_WORKERS}, Buffer={DOWNLOAD_COMPLETE_BUFFER}/{UPLOAD_QUEUE_BUFFER}")
            logger.info(f"Command line arguments: {vars(args)}")

            # --- Initialize DB, SmugMug, Google Photos & Check Config ---
            try:
                 db_manager = DatabaseManager(db_file=args.db_file); db_manager_global = db_manager; logger.info("DB manager initialized.")
            except Exception as e:
                 logger.critical(f"Failed init DB manager: {e}", exc_info=True); sys.exit(1)

            try:
                smugmug = SmugMug(config_file='smugmug_config.json'); smugmug.load_config()
            except FileNotFoundError:
                 logger.warning(f"SmugMug config file missing.");
                 if smugmug.generate_default_config():
                     sys.exit(0)
                 else:
                     logger.critical("Failed generate default SmugMug config."); sys.exit(1)
            except Exception as e:
                 logger.critical(f"Failed load SmugMug config: {e}", exc_info=True); sys.exit(1)

            current_smugmug_album_arg = args.smugmug_album; current_smugmug_folder_arg = args.smugmug_folder; current_google_album_arg = args.google_photos_album_id
            if args.process_heic:
                if smugmug.config:
                    smugmug.config['process_heic'] = True; logger.info("Override: HEIC processing enabled.")
                else:
                    logger.error("Cannot apply HEIC override."); sys.exit(1)
            if current_smugmug_album_arg:
                 logger.info(f"Override: SM Album: '{current_smugmug_album_arg}'");
                 if smugmug.config:
                     smugmug.config['album_name'] = current_smugmug_album_arg; smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']; smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
                 else:
                     logger.error("Cannot apply album override."); sys.exit(1)
            if current_smugmug_folder_arg:
                 logger.info(f"Override: SM Folder: '{current_smugmug_folder_arg}'");
                 if smugmug.config:
                     smugmug.config['folder_name'] = current_smugmug_folder_arg
                 else:
                     logger.error("Cannot apply folder override."); sys.exit(1)

            if not smugmug.check_config_and_authenticate():
                logger.critical("SmugMug auth/config failed."); sys.exit(1)
            logger.info("SmugMug init and auth successful.")

            current_target_album_name = smugmug.config.get('album_name') if smugmug.config else None; current_target_folder_path = smugmug.config.get('folder_name') if smugmug.config else None
            current_target_album_key = smugmug.config.get('album_key') if smugmug.config else None; current_target_album_uri = smugmug.config.get('album_api_uri') if smugmug.config else None

            if current_target_album_name and current_target_album_name != DEFAULT_SMUGMUG_CONFIG['album_name']:
                 logger.info(f"Ensuring SM album '{current_target_album_name}' exists in folder '{current_target_folder_path or 'root'}'...")
                 if not smugmug.get_or_create_album_in_path(current_target_album_name, current_target_folder_path):
                     logger.critical("Failed find/create target album."); sys.exit(1)
                 current_target_album_key = smugmug.album_key; current_target_album_uri = smugmug.album_api_uri
            elif current_target_album_key and current_target_album_key != DEFAULT_SMUGMUG_CONFIG['album_key']:
                 logger.info(f"Using existing SM album key: {current_target_album_key}"); smugmug.album_key = current_target_album_key; smugmug.album_api_uri = current_target_album_uri; smugmug.album_name = None
            else:
                 logger.critical("No valid target SM album specified."); sys.exit(1)
            logger.info(f"Current Target - SM Key: {current_target_album_key}, URI: {current_target_album_uri}, Folder: '{current_target_folder_path or 'root'}'")

            try:
                google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json'); google_photos_instance_global = google_photos
                if not google_photos.is_authenticated():
                    logger.critical("GP module not authenticated."); sys.exit(1)
                logger.info("GP init and auth successful.")
            except GoogleCredentialsNotFoundError as e:
                print(f"\nError: {e}"); sys.exit(1)
            except Exception as e:
                logger.critical(f"Failed init GP module: {e}", exc_info=True); sys.exit(1)

            # --- Populate Database or Check Config Snapshot ---
            is_initial_run = False; total_db_items = db_manager.get_item_count(); stored_config = db_manager.get_stored_config()
            if total_db_items == 0 or args.force_refresh_list:
                is_initial_run = True
                if args.force_refresh_list:
                    logger.warning("Force refresh: Re-fetching list...")
                else:
                    logger.info("Database empty. Fetching list...")
                print("Fetching initial media list from Google Photos...")
                photos_list = google_photos.get_photos(current_google_album_arg)
                logger.info(f"Fetched {len(photos_list)} items.")
                if photos_list:
                     if not db_manager.save_initial_config(current_target_album_key, current_target_album_uri, current_target_folder_path, current_google_album_arg):
                         logger.error("Failed save initial config snapshot.")
                     added_count = db_manager.add_item_batch(photos_list, current_target_album_key); logger.info(f"Populated DB with {added_count} items.")
                     total_db_items = db_manager.get_item_count()
                else:
                     logger.info("No items found in Google Photos source."); sys.exit(0)
            else:
                logger.info(f"DB contains {total_db_items} items. Checking config...")
                print(f"Found {total_db_items} items in DB. Checking config...")
                if stored_config:
                    stored_sm_key = stored_config.get('smugmug_album_key'); stored_sm_folder = stored_config.get('smugmug_folder_name'); stored_gp_album = stored_config.get('google_album_id')
                    current_folder_norm = current_target_folder_path if current_target_folder_path else None; stored_folder_norm = stored_sm_folder if stored_sm_folder else None
                    current_gp_album_norm = current_google_album_arg if current_google_album_arg else None; stored_gp_album_norm = stored_gp_album if stored_gp_album else None
                    mismatch = False
                    if current_target_album_key != stored_sm_key:
                        logger.warning(f"SM Key mismatch! Current='{current_target_album_key}', Stored='{stored_sm_key}'"); mismatch = True
                    if current_folder_norm != stored_folder_norm:
                        logger.warning(f"SM Folder mismatch! Current='{current_folder_norm or 'Root'}', Stored='{stored_folder_norm or 'Root'}'"); mismatch = True
                    if current_gp_album_norm != stored_gp_album_norm:
                        logger.warning(f"GP Album ID mismatch! Current='{current_gp_album_norm or 'Library'}', Stored='{stored_gp_album_norm or 'Library'}'"); mismatch = True
                    if mismatch:
                        logger.warning("="*60); logger.warning("CONFIG MISMATCH! CONTINUING WITH STORED settings:"); logger.warning(f"  - SM Key: {stored_sm_key}"); logger.warning(f"  - SM Folder: {stored_folder_norm or 'Root'}"); logger.warning(f"  - GP Album: {stored_gp_album_norm or 'Entire Library'}"); logger.warning("To use NEW settings, stop (Ctrl+C) and delete DB or use --force-refresh-list"); logger.warning("="*60)
                        print("\n" + "="*60); print("WARNING: CONFIG MISMATCH!"); print(">>> CONTINUING WITH STORED SETTINGS FROM DATABASE <<<"); print(f"    SM Key: {stored_sm_key}"); print(f"    SM Folder: {stored_folder_norm or 'Root'}"); print(f"    GP Album: {stored_gp_album_norm or 'Entire Library'}"); print("\nTo use NEW settings, stop (Ctrl+C) and delete DB or use --force-refresh-list"); print("="*60 + "\n"); time.sleep(5)
                        smugmug.album_key = stored_sm_key; smugmug.album_api_uri = stored_config.get('smugmug_album_uri'); smugmug.folder_name = stored_folder_norm; smugmug.album_name = None
                        current_target_album_key = stored_sm_key # Update 'current' vars to reflect stored values being used
                    else:
                        logger.info("Config matches snapshot."); print("Config matches stored state. Resuming...")
                else:
                    logger.warning("Existing DB found, no config snapshot. Proceeding with CURRENT settings."); print("\nWARNING: Existing DB found without config snapshot. Proceeding with CURRENT settings.")
                    if not db_manager.save_initial_config(current_target_album_key, current_target_album_uri, current_target_folder_path, current_google_album_arg):
                        logger.error("Failed save current config snapshot.")

            # --- Reset Errors if Requested ---
            if args.reset_errors:
                db_manager.reset_failed_items(); total_db_items = db_manager.get_item_count()

            # --- Get Items to Process ---
            logger.info("Fetching items to process from database...")
            items_to_process_list = db_manager.get_items_to_process(retry_errors=args.retry_errors)
            total_items_to_process = len(items_to_process_list) # Update total count for this run
            logger.info(f"Found {total_items_to_process} items requiring processing.")
            if total_items_to_process == 0:
                 logger.info("No items require processing."); print("No items require processing.")
                 # Proceed to final summary (no loop needed)
            # else: Items need processing, proceed to pipeline


            # --- Pipeline Processing ---
            # Reset counters for this run
            processed_in_run = 0; uploaded_in_run = 0; duplicates_in_run = 0; skipped_in_run = 0; errors_in_run = 0
            processed_ids = set() # Track IDs successfully processed by final stage
            submitted_futures = {} # Track all submitted futures {future: stage_name-id}
            active_download_ids = set()
            active_processing_ids = set()
            active_upload_ids = set()


            if total_items_to_process > 0:
                items_iterator = iter(items_to_process_list) # Iterator for items list

                # --- Submit Initial Tasks ---
                logger.info("Submitting initial tasks to pipeline...")
                # Submit initial downloads, limited by buffer size
                for _ in range(min(DOWNLOAD_COMPLETE_BUFFER, total_items_to_process)):
                     if shutdown_event.is_set():
                         break
                     try:
                          item = next(items_iterator)
                          item_id = item['google_id']
                          item['_should_process_heic'] = args.process_heic or (smugmug.config and smugmug.config.get('process_heic', False))
                          future = download_executor.submit(download_stage_worker, item, google_photos, db_manager, download_complete_queue, upload_queue)
                          submitted_futures[future] = f"Download-{item_id}"
                          active_download_ids.add(item_id)
                     except StopIteration:
                         break

                # --- Main Pipeline Loop ---
                while processed_in_run < total_items_to_process:
                    if shutdown_event.is_set() and not submitted_futures:
                         logger.warning("Shutdown requested and all tasks completed or failed. Exiting loop.")
                         break

                    # --- Submit Processing Tasks ---
                    try:
                        # Non-blocking check if buffer allows more processing tasks
                        if len(active_processing_ids) < MAX_PROCESSING_WORKERS:
                            item_details, temp_path, refreshed = download_complete_queue.get_nowait()
                            item_id = item_details['google_id']
                            logger.debug(f"Submitting processing task for {item_id[:8]}")
                            future = processing_executor.submit(processing_stage_worker, item_details, temp_path, refreshed, smugmug, db_manager, upload_queue)
                            submitted_futures[future] = f"Process-{item_id}"
                            active_processing_ids.add(item_id)
                            active_download_ids.discard(item_id) # No longer just downloading
                            download_complete_queue.task_done()
                    except queue.Empty:
                        pass # No items ready for processing yet
                    except Exception as e:
                        logger.error(f"Error submitting processing task: {e}", exc_info=True)


                    # --- Submit Upload Tasks ---
                    try:
                        # Non-blocking check if buffer allows more upload tasks
                        if len(active_upload_ids) < MAX_UPLOAD_WORKERS:
                            item_details, temp_path = upload_queue.get_nowait()
                            item_id = item_details['google_id']
                            logger.debug(f"Submitting upload task for {item_id[:8]}")
                            future = upload_executor.submit(upload_stage_worker, item_details, temp_path, smugmug, db_manager, google_photos, args.delete_from_google, args.dry_run)
                            submitted_futures[future] = f"Upload-{item_id}"
                            active_upload_ids.add(item_id)
                            active_processing_ids.discard(item_id) # No longer just processing
                            upload_queue.task_done()
                    except queue.Empty:
                        pass # No items ready for upload yet
                    except Exception as e:
                        logger.error(f"Error submitting upload task: {e}", exc_info=True)


                    # --- Submit More Download Tasks (if buffer allows and items remain) ---
                    # Check buffer based on items in queues and active processing/upload
                    items_in_flight = download_complete_queue.qsize() + upload_queue.qsize() + len(active_processing_ids) + len(active_upload_ids)
                    if items_in_flight < DOWNLOAD_COMPLETE_BUFFER and not shutdown_event.is_set():
                         try:
                              item = next(items_iterator)
                              item_id = item['google_id']
                              item['_should_process_heic'] = args.process_heic or (smugmug.config and smugmug.config.get('process_heic', False))
                              logger.debug(f"Submitting download task for {item_id[:8]} (buffer refill)")
                              future = download_executor.submit(download_stage_worker, item, google_photos, db_manager, download_complete_queue, upload_queue)
                              submitted_futures[future] = f"Download-{item_id}"
                              active_download_ids.add(item_id)
                         except StopIteration:
                              # logger.debug("All items submitted to download stage.") # Can be noisy
                              pass # No more items left to submit


                    # --- Check Completed Futures ---
                    if not submitted_futures:
                         if download_complete_queue.empty() and upload_queue.empty():
                              logger.debug("No active futures and queues empty, brief sleep.")
                              time.sleep(0.5)
                         if processed_in_run >= total_items_to_process:
                             break # Exit if really done
                         continue # Go back to check queues/submit tasks


                    done, _ = concurrent.futures.wait(submitted_futures.keys(), timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        stage_info = submitted_futures.pop(future) # Remove completed future
                        parts = stage_info.split('-', 1)
                        stage_name = parts[0]
                        item_id = parts[1] # Full ID stored here now

                        try:
                            future.result() # Check for exceptions raised in worker
                            # Update active sets and potentially summary counts
                            if stage_name == "Download":
                                active_download_ids.discard(item_id)
                            elif stage_name == "Process":
                                active_processing_ids.discard(item_id)
                            elif stage_name == "Upload":
                                 active_upload_ids.discard(item_id)
                                 processed_in_run += 1 # Count item as fully processed *after* upload attempt
                                 processed_ids.add(item_id) # Track successfully processed ID
                                 # Query DB for final status to update summary counts
                                 # Need to handle potential None return if DB access fails
                                 final_status_info = db_manager.get_item_details(item_id) if db_manager else None
                                 if final_status_info:
                                      final_status = final_status_info['status']
                                      if final_status == STATUS_UPLOADED_SUCCESS:
                                          uploaded_in_run += 1
                                      elif final_status in [STATUS_DUPLICATE_FILENAME, STATUS_DUPLICATE_HASH]:
                                          duplicates_in_run += 1
                                      elif final_status in [STATUS_SKIPPED_FILTER, STATUS_SKIPPED_HEIC]:
                                          skipped_in_run += 1
                                      elif final_status in ERROR_STATUSES:
                                          errors_in_run += 1
                                 else:
                                     logger.warning(f"Could not retrieve final status for {item_id[:8]} after upload worker.")

                        except Exception as exc:
                            logger.error(f"Exception caught from worker future '{stage_info}': {exc}", exc_info=True)
                            errors_in_run += 1
                            processed_in_run += 1 # Count as processed (with error)
                            processed_ids.add(item_id) # Track ID even if errored
                            # Remove from active sets
                            if stage_name == "Download":
                                active_download_ids.discard(item_id)
                            elif stage_name == "Process":
                                active_processing_ids.discard(item_id)
                            elif stage_name == "Upload":
                                active_upload_ids.discard(item_id)

                    # Check if we are done processing all items targeted in this run
                    if processed_in_run >= total_items_to_process:
                         logger.info("All targeted items have been processed (or attempted).")
                         break

                # --- End of Pipeline Loop ---
                logger.info("Pipeline processing loop finished.")

                # --- Wait for remaining tasks (especially if shutdown occurred) ---
                if submitted_futures:
                    logger.info(f"Waiting for {len(submitted_futures)} remaining tasks...")
                    # Wait for all futures to complete or be cancelled
                    concurrent.futures.wait(submitted_futures.keys())
                    logger.info("All remaining tasks completed or cancelled.")
                    # Check for final exceptions (might double count errors if shutdown happened mid-task)
                    for future in list(submitted_futures.keys()): # Iterate over a copy
                        stage_info = submitted_futures.pop(future, "Unknown-Future") # Use pop with default
                        try:
                            future.result()
                        except Exception as exc:
                            logger.error(f"Exception caught post-loop for '{stage_info}': {exc}", exc_info=True)


            # --- Final Summary --- Placed Correctly Inside Try Block ---
            total_duration = time.time() - start_time
            logger.info("=" * 60)
            logger.info("Run Summary:")
            logger.info(f"  Items Processed in this Run:   {processed_in_run}")
            logger.info(f"  Uploaded in this Run:        {uploaded_in_run}")
            logger.info(f"  Marked as Duplicate this Run: {duplicates_in_run}")
            logger.info(f"  Skipped in this Run:         {skipped_in_run}")
            logger.info(f"  Errors in this Run:          {errors_in_run}")
            logger.info(f"  Total processing time:       {total_duration:.2f} seconds")
            logger.info("-" * 60)
            logger.info("Overall Database Stats:")
            # Ensure db_manager exists before getting stats
            final_stats = db_manager.get_stats() if db_manager else {}
            for status, count in sorted(final_stats.items()):
                logger.info(f"  - {status}: {count}")
            logger.info("=" * 60)

            print("\n" + "=" * 60)
            print("Run Summary:")
            print(f"- Processed in Run: {processed_in_run}/{total_items_to_process}")
            print(f"- Uploaded{' (Dry Run)' if args.dry_run else ''}: {uploaded_in_run}")
            print(f"- Duplicates Found: {duplicates_in_run}")
            print(f"- Skipped: {skipped_in_run}")
            print(f"- Errors: {errors_in_run}")

            # --- Determine Final Status and Handle DB Rename ---
            run_completed_successfully = (errors_in_run == 0 and not shutdown_requested)
            final_db_filename = args.db_file

            if run_completed_successfully:
                print("- Status: Completed run successfully")
                # Ensure db_manager exists before querying
                remaining_items = db_manager.get_items_to_process(retry_errors=False) if db_manager else []
                if not remaining_items:
                    logger.info("All targeted items processed successfully. Run complete.")
                    print("All items processed successfully.")
                    logger.info("Attempting to rename completed database file...")
                    if db_manager_global:
                        db_manager_global.close(); logger.info("Closed DB before renaming.")
                    else:
                        logger.warning("DB manager instance not found.")
                    base_db_name, db_ext = os.path.splitext(args.db_file)
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    new_db_filename = f"{base_db_name}_completed_{timestamp}{db_ext}"
                    try:
                        if os.path.exists(args.db_file):
                            os.rename(args.db_file, new_db_filename)
                            logger.info(f"Successfully renamed database to: {new_db_filename}")
                            print(f"Database renamed to: {new_db_filename}")
                            final_db_filename = new_db_filename
                            cleanup_handled_in_try = True # Prevent cleanup in finally
                            release_lock() # Release lock manually
                        else:
                            logger.warning(f"DB file {args.db_file} not found for renaming.")
                    except OSError as e:
                        logger.error(f"Failed to rename database file: {e}", exc_info=True)
                        print(f"\nERROR: Failed to rename completed database file: {e}")
                else:
                     logger.warning(f"Run completed without errors, but {len(remaining_items)} items still require processing. DB not renamed.")
                     print(f"Run completed without errors, but {len(remaining_items)} items still require processing. Rerun to continue.")
            elif shutdown_requested:
                print("- Status: Terminated by user")
            else:
                print(f"- Status: Completed run with {errors_in_run} errors")

            print(f"- Run Time: {total_duration:.2f} sec")
            print("-" * 60)
            print("Overall Database Stats:")
            for status, count in sorted(final_stats.items()):
                 if count > 0:
                     print(f"- {status}: {count}")
            print(f"- Detailed Log: {LOG_FILE}")
            print(f"- Database File: {final_db_filename}") # Show final name
            print("=" * 60)
            # --- End of Summary Block ---

    # --- End of main 'with' block for executors ---

    # --- Exceptions from the main try block (inside 'with') are caught here ---
    except KeyboardInterrupt:
        if not shutdown_requested:
                logger.warning("Keyboard interrupt detected directly in main.")
                print("\nKeyboard Interrupt. Shutting down...")
                shutdown_event.set() # Signal threads
                shutdown_requested = True
    except Exception as e:
        logger.critical(f"Critical unexpected error in main execution: {e}", exc_info=True)
        print(f"\nCritical Error: {e}. Check log '{LOG_FILE}'.", file=sys.stderr)
        shutdown_event.set() # Signal threads on critical error
        shutdown_requested = True
    # --- End of main try block's except clauses ---

    # --- This finally block is now correctly aligned with the outer try ---
    finally:
        # --- Cleanup ---
        logger.info("Executing final cleanup...")
        # Ensure shutdown event is set if not already (e.g., normal exit)
        shutdown_event.set()
        # Only run full cleanup if it wasn't handled successfully in the try block (DB rename)
        if not cleanup_handled_in_try:
            cleanup(google_photos_instance_global, db_manager_global)
        else:
            logger.info("Skipping normal cleanup as DB was successfully renamed.")

        exit_code = 1 if errors_in_run > 0 or shutdown_requested else 0
        logger.info(f"Exiting script with code {exit_code}.")
        sys.exit(exit_code)

if __name__ == "__main__":
    main()
