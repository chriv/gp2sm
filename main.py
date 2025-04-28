# Google Photos to SmugMug Transfer Script (v2.0)
# - Added custom PROGRESS log level with distinct color.
# - Changed progress calculation to show overall progress (processed / total_db).
# - Reverted colorlog setup *exactly* to v1.9 structure (plus threadName).
# - Changed console handler back to sys.stdout.
# - Replaced ALL print statements with logger calls for progress, summary, and status.
# - Fixed SyntaxError in worker try/except blocks.
# Implements parallel processing using worker threads,
# each handling the full lifecycle (Download -> Hash -> Check -> Upload) for one item.

__version__ = "2.0" # Version kept as requested

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
import queue # Still needed for potential future use, but not pipeline
import concurrent.futures
import threading
import traceback # For detailed error logging in threads

# Third-party imports
# Ensure colorlog is installed: pip install colorlog
try:
    import colorlog
except ImportError:
    # This print is necessary as logger is not configured yet
    print("Error: 'colorlog' package not found. Please install it using: pip install colorlog", file=sys.stderr)
    sys.exit(1)


# Local module imports
# Make sure these files are in the same directory or your Python path
try:
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
except ImportError as e:
    # This print is necessary as logger might depend on these modules
    print(f"Error importing local modules: {e}", file=sys.stderr)
    print("Please ensure google_photos_module.py, smugmug_module.py, and database_manager.py are present.", file=sys.stderr)
    sys.exit(1)


# --- Constants ---
LOG_FILE = "gp2sm_transfer.log"
LOCK_FILE = "gp2sm.lock"
LOG_ID_TRUNCATE_LEN = 8
# --- Concurrency Settings ---
MAX_WORKERS = 5 # Default number of concurrent worker threads
# --- Custom Log Level ---
PROGRESS_LEVEL_NUM = 15 # Between DEBUG (10) and INFO (20)

# --- Global Variables ---
logger = None
shutdown_requested = False
google_photos_instance_global = None
db_manager_global = None
# Use events for signaling shutdown across threads
shutdown_event = threading.Event()
# Counters for summary (query DB at the end for accuracy)
uploaded_in_run = 0
duplicates_in_run = 0
skipped_in_run = 0
errors_in_run = 0

# --- Signal Handling ---
def signal_handler(sig, frame):
    """Handles termination signals (Ctrl+C, etc.) for graceful shutdown."""
    global shutdown_requested, logger, shutdown_event
    if not shutdown_requested:
        signal_name = f"Signal {sig}"
        try:
            # Attempt to get the signal name (e.g., SIGINT)
            signal_name = signal.Signals(sig).name
        except ValueError:
            # Keep the numeric signal if name lookup fails
            pass
        # Use logger if available, otherwise fallback to print
        log_func = getattr(logger, 'warning', lambda msg: print(msg, file=sys.stderr))
        log_func(f"Received signal {signal_name}. Initiating graceful shutdown...")
        # Log shutdown message
        log_func(f">>> Signal {signal_name} received. Stopping submission of new tasks... <<<")
        shutdown_requested = True
        shutdown_event.set() # Signal threads/main loop to stop
    else:
        # If shutdown already requested, log debug message
        if logger:
            logger.debug(f"Shutdown already in progress. Received signal {sig} again.")
        else: # Logger might not be configured if signal received very early
             print(">>> Shutdown already requested. Please wait. <<<", file=sys.stderr)


# --- Logging Setup ---
def setup_logging(debug=False):
    """Configures logging to both console (stdout) and a rotating file."""
    global logger, PROGRESS_LEVEL_NUM

    # Add custom PROGRESS level
    logging.addLevelName(PROGRESS_LEVEL_NUM, "PROGRESS")
    def progress(self, message, *args, **kws):
        # Yes, logger takes its '*args' as 'args'.
        if self.isEnabledFor(PROGRESS_LEVEL_NUM):
            self._log(PROGRESS_LEVEL_NUM, message, args, **kws)
    logging.Logger.progress = progress # Add the method to the Logger class

    log_level = logging.DEBUG if debug else logging.INFO
    # Ensure PROGRESS level is always enabled if INFO is enabled
    if log_level > PROGRESS_LEVEL_NUM:
        log_level = PROGRESS_LEVEL_NUM

    logger = logging.getLogger() # Get root logger
    # Clear existing handlers to prevent duplicate logs if re-configured
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(log_level) # Set minimum level for the logger

    # Define log format based on v1.9 snippet, adding threadName
    # Use %(message_log_color)s to allow secondary colors for message part
    # Added %(threadName)s inside the brackets
    console_format = ('%(asctime)s - %(log_color)s%(levelname)-8s%(reset)s - '
                      '[%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - '
                      '%(message_log_color)s%(message)s%(reset)s') # Final reset included

    # Configure console handler (using colorlog, output to stdout)
    # Use exact formatter setup from v1.9 snippet, adding PROGRESS color
    console_formatter = colorlog.ColoredFormatter(
        console_format, # Use the single format string
        datefmt='%Y-%m-%d %H:%M:%S',
        reset=True, # reset=True adds reset at the end
        log_colors={
            'DEBUG':    'cyan',
            'INFO':     'green',
            'PROGRESS': 'blue', # Assign color for PROGRESS level
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red,bg_white',
        },
        # secondary_log_colors matching v1.9 snippet + PROGRESS reset
        secondary_log_colors={
            'message': {
                'PROGRESS': 'reset', # Ensure PROGRESS message part uses default color
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'red', # Background is handled by log_colors
            }
            # INFO and DEBUG messages will use default color due to %(reset)s after levelname
        },
        style='%'
    )
    # Changed handler back to sys.stdout as per user request
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    # Console handler level respects the debug flag, but ensures PROGRESS is shown if INFO is
    console_handler_level = logging.DEBUG if debug else logging.INFO
    if console_handler_level > PROGRESS_LEVEL_NUM:
         console_handler_level = PROGRESS_LEVEL_NUM
    console_handler.setLevel(console_handler_level)
    logger.addHandler(console_handler)

    # Configure file handler (rotating file, always logs DEBUG level and above)
    # File handler uses a non-colored format, adapted to include threadName
    file_format = '%(asctime)s - %(levelname)-8s - [%(threadName)s:%(name)s:%(funcName)s:%(lineno)d] - %(message)s'
    file_formatter = logging.Formatter(file_format, datefmt='%Y-%m-%d %H:%M:%S')
    try:
        # Rotate log file when it reaches 5MB, keep 5 backup files
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG) # Log everything (including PROGRESS) to the file
        logger.addHandler(file_handler)
    except Exception as e:
        # Log error to stderr if file logging fails
        # Use print here as the logger file handler itself failed
        print(f"Warning: Could not configure file logging to '{LOG_FILE}': {e}", file=sys.stderr)
        if logger: # Log to console handler if it was set up
            logger.error(f"Failed to set up file logging handler: {e}", exc_info=True)

    # Reduce verbosity of noisy third-party libraries
    for lib_logger_name in ["googleapiclient.discovery_cache", "google.auth.transport.requests",
                            "urllib3.connectionpool", "requests_oauthlib.oauth1_session"]:
        logging.getLogger(lib_logger_name).setLevel(logging.WARNING)
    # Set level for our own modules based on debug flag
    logging.getLogger("database_manager").setLevel(log_level)
    logging.getLogger("google_photos_module").setLevel(log_level)
    logging.getLogger("smugmug_module").setLevel(log_level)


# --- Lock File Management ---
def acquire_lock():
    """Creates a lock file to prevent multiple instances from running."""
    try:
        # Attempt to create the lock file exclusively
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd) # Close the file descriptor immediately after creation
        # Logger might not be configured yet if called very early, check first
        if logger:
            logger.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        # Lock file already exists, another instance might be running
        log_msg = f"Lock file '{LOCK_FILE}' already exists. Another instance may be running."
        if logger:
            logger.error(log_msg)
        else: # Fallback if logger not ready
            print(f"Error: {log_msg}", file=sys.stderr)
            print("If sure no other instance is running, delete the lock file and try again.", file=sys.stderr)
        return False
    except OSError as e:
        # Other OS error occurred during lock file creation
        log_msg = f"Error acquiring lock file '{LOCK_FILE}': {e}"
        if logger:
            logger.error(log_msg, exc_info=True)
        else: # Fallback if logger not ready
            print(f"Error: {log_msg}", file=sys.stderr)
        return False

def release_lock():
    """Removes the lock file if it exists."""
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            if logger:
                logger.info(f"Released lock file: {LOCK_FILE}")
        except OSError as e:
            # Log warning if lock file removal fails
            if logger:
                logger.warning(f"Could not remove lock file '{LOCK_FILE}': {e}", exc_info=True)
            else: # Fallback if logger not ready
                print(f"Warning: Could not remove lock file '{LOCK_FILE}': {e}", file=sys.stderr)


# --- Cleanup Function ---
def cleanup(google_photos_instance, db_manager_instance):
    """Performs cleanup actions like closing DB and removing temp files."""
    # Use logger methods if available, otherwise fallback to print (to stderr)
    log_func_info = getattr(logger, 'info', lambda msg: print(f"INFO: {msg}", file=sys.stderr))
    log_func_debug = getattr(logger, 'debug', lambda msg: print(f"DEBUG: {msg}", file=sys.stderr))
    log_func_error = getattr(logger, 'error', lambda msg: print(f"ERROR: {msg}", file=sys.stderr))

    log_func_info("--- Running cleanup procedures ---")
    # Close database connection if manager exists and has a close method
    if db_manager_instance and hasattr(db_manager_instance, 'close') and callable(db_manager_instance.close):
         try:
             log_func_debug("Closing database connection...")
             db_manager_instance.close()
         except Exception as e:
             log_func_error(f"Error closing database connection: {e}", exc_info=True)
    else:
         log_func_debug("No database manager instance provided or close method missing for cleanup.")

    # Cleanup Google Photos temporary directory if instance exists and has method
    if google_photos_instance and hasattr(google_photos_instance, 'cleanup_temp_dir') and callable(google_photos_instance.cleanup_temp_dir):
        try:
            log_func_debug("Calling Google Photos temporary directory cleanup...")
            google_photos_instance.cleanup_temp_dir()
        except Exception as e:
            log_func_error(f"Error during Google Photos temp directory cleanup: {e}", exc_info=True)
    else:
        log_func_debug("No Google Photos instance provided or cleanup method missing for cleanup.")

    # Always release the lock file during cleanup
    release_lock()
    log_func_info("--- Cleanup complete ---")


# --- Item Processing Worker Function ---
def process_item_worker(item_details, google_photos, smugmug, db_manager, args):
    """
    Worker function executed by each thread. Handles the entire lifecycle
    for a single media item: Download -> Hash -> Check -> Upload.
    Updates the database status at each significant step.

    Args:
        item_details (dict): Dictionary containing details of the media item from the DB.
        google_photos (GooglePhotos): Initialized GooglePhotos instance.
        smugmug (SmugMug): Initialized SmugMug instance.
        db_manager (DatabaseManager): Initialized DatabaseManager instance.
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        tuple: (google_id, final_status) indicating the outcome for this item.
               google_id might be None if essential data was missing.
    """
    # Access global shutdown event to allow early exit
    global shutdown_event

    # Safely extract essential details from the item dictionary
    google_id = item_details.get('google_id')
    filename = item_details.get('filename')
    mime_type = item_details.get('mime_type')
    current_md5_hash = item_details.get('md5_hash') # May be None initially

    # Determine media type characteristics
    is_video = mime_type.startswith('video/') if mime_type else False
    # Check filename case-insensitively for .heic extension
    is_heic = filename.lower().endswith('.heic') if filename else False
    # Determine if HEIC files should be processed based on args or SmugMug config
    should_process_heic = args.process_heic or (smugmug.config and smugmug.config.get('process_heic', False))

    # --- Pre-check: Ensure essential data exists ---
    if not google_id or not filename or not mime_type:
        # Log error if core data is missing
        logger.error(f"Worker skipped item due to missing core data: ID={google_id}, Filename={filename}, Mime={mime_type}")
        # Attempt to update DB status for missing data if we have an ID
        if google_id:
            db_manager.update_item_status(google_id, STATUS_ERROR_MISSING_DATA, "Item missing filename or mimeType in DB")
        # Return error status; google_id might be None here
        return google_id, STATUS_ERROR_MISSING_DATA

    # Create a truncated ID for cleaner logging
    truncated_id = f"{google_id[:LOG_ID_TRUNCATE_LEN]}...{google_id[-LOG_ID_TRUNCATE_LEN:]}" if len(google_id) > LOG_ID_TRUNCATE_LEN * 2 else google_id
    log_identifier = f"Worker (ID: {truncated_id}, File: '{filename}')"
    # Initial log message commented out to reduce noise; uncomment if needed for debugging start of each item
    # logger.info(f"{log_identifier}: Starting processing.")

    temp_file_path = None # Path to downloaded file, initially None
    final_status = item_details.get('status', STATUS_PENDING) # Start with current status from DB

    try:
        # --- Check for Shutdown Signal ---
        if shutdown_event.is_set():
            # Log warning only if the item wasn't already in a terminal or error state
            if final_status not in TERMINAL_STATUSES and final_status not in ERROR_STATUSES:
                 logger.warning(f"{log_identifier}: Shutdown signalled before processing started. Skipping.")
            # Return the item's current status as no work was done
            return google_id, final_status

        # --- Apply Command-Line Filters ---
        if args.ignore_photos and not is_video:
             logger.info(f"{log_identifier}: Marked to skip (Photo filter active).")
             db_manager.update_item_status(google_id, STATUS_SKIPPED_FILTER, error_message="Skipped via --ignore-photos")
             return google_id, STATUS_SKIPPED_FILTER
        if args.ignore_videos and is_video:
             logger.info(f"{log_identifier}: Marked to skip (Video filter active).")
             db_manager.update_item_status(google_id, STATUS_SKIPPED_FILTER, error_message="Skipped via --ignore-videos")
             return google_id, STATUS_SKIPPED_FILTER

        # --- HEIC File Handling ---
        if is_heic:
            if not should_process_heic:
                # If HEIC processing is disabled, skip the file
                logger.info(f"{log_identifier}: Marked to skip (HEIC processing disabled).")
                db_manager.update_item_status(google_id, STATUS_SKIPPED_HEIC, error_message="HEIC processing not enabled")
                return google_id, STATUS_SKIPPED_HEIC
            else:
                # HEIC processing enabled, proceed (no duplicate check possible)
                # logger.warning(f"{log_identifier}: Processing HEIC (no duplicate check).") # Optional: Log HEIC processing start
                pass # Continue processing

        # --- Download Step (Conditional) ---
        # Determine if download is necessary based on type and current state
        needs_download = (not is_video and not is_heic and not current_md5_hash) or \
                         is_video or \
                         (is_heic and should_process_heic)

        refreshed_details = None # To store potentially updated details from download

        if needs_download:
            logger.debug(f"{log_identifier}: Download required. Calling download function...")
            # Call the download method, which handles retries and potential detail refresh
            temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details)

            # Check if download was successful
            if not temp_file_path:
                logger.error(f"{log_identifier}: Download failed.")
                # Update DB status to reflect download error
                db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Download function returned failure")
                return google_id, STATUS_ERROR_DOWNLOAD # Exit processing for this item

            # If download refreshed item details (e.g., new baseUrl), update the database
            if refreshed_details:
                 logger.debug(f"{log_identifier}: Details refreshed during download. Updating DB.")
                 new_base_url = refreshed_details.get('baseUrl')
                 # Only update metadata if it exists in the refreshed details
                 new_metadata = refreshed_details.get('mediaMetadata')
                 # Keep old metadata if no new metadata was fetched
                 new_metadata_json = json.dumps(new_metadata) if new_metadata else item_details.get('media_metadata_json')
                 db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                 refreshed_details = None # Reset flag after processing

        # --- Hashing Step (Conditional) ---
        # Only hash if: Not video, not HEIC, AND no hash exists yet in DB
        if not is_video and not is_heic and not current_md5_hash:
            # Ensure the downloaded file exists before attempting to hash
            if not temp_file_path or not os.path.exists(temp_file_path):
                 # This implies download was needed but failed, or file disappeared
                 logger.error(f"{log_identifier}: Temp file missing for hashing (expected path: {temp_file_path}). Download may have failed silently.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Temp file missing before hash calculation")
                 return google_id, STATUS_ERROR_DOWNLOAD # Exit processing

            logger.info(f"{log_identifier}: Calculating MD5 hash...")
            # Calculate MD5 hash using the SmugMug module's utility function
            calculated_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')

            # Check if hashing was successful
            if not calculated_hash:
                logger.error(f"{log_identifier}: MD5 hash calculation failed.")
                db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, "MD5 calculation failed")
                # Cleanup temp file if hashing failed
                if temp_file_path and os.path.exists(temp_file_path):
                    # Use proper try/except block structure
                    try:
                        os.remove(temp_file_path)
                        logger.debug(f"{log_identifier}: Cleaned temp file after hashing error.")
                    except OSError as e:
                        logger.warning(f"{log_identifier}: Failed to clean temp file {temp_file_path} after hashing error: {e}")
                return google_id, STATUS_ERROR_HASHING # Exit processing
            else:
                # Hashing successful, update local variable and database
                logger.debug(f"{log_identifier}: Calculated MD5: {calculated_hash}. Updating DB.")
                current_md5_hash = calculated_hash # Update local variable for subsequent checks
                db_manager.update_item_status(google_id, STATUS_HASHED, md5_hash=current_md5_hash)
                final_status = STATUS_HASHED # Update intermediate status

        # --- SmugMug Duplicate Check Step (Conditional) ---
        # Skip duplicate check if it's a HEIC file being processed
        if not (is_heic and should_process_heic):
            exists_on_smugmug = False
            log_reason = "" # Reason for check (filename or hash)
            # Use the confirmed album key from the initialized SmugMug object
            target_album_key_for_check = smugmug.album_key
            if not target_album_key_for_check:
                 # Should not happen if initialization succeeded, but check defensively
                 logger.error(f"{log_identifier}: SmugMug target album key missing. Cannot check for duplicates.")
                 db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, "SM album key missing during duplicate check")
                 # Cleanup temp file if it exists
                 if temp_file_path and os.path.exists(temp_file_path):
                     # Use proper try/except block structure
                     try:
                         os.remove(temp_file_path)
                         logger.debug(f"{log_identifier}: Cleaned temp file after SM API error (missing key).")
                     except OSError as e:
                         logger.warning(f"{log_identifier}: Failed to clean temp file {temp_file_path} after SM API error: {e}")
                 return google_id, STATUS_ERROR_SMUGMUG_API # Exit processing

            logger.info(f"{log_identifier}: Checking SmugMug album '{target_album_key_for_check}' for duplicates...")
            # Perform check based on media type
            if is_video:
                 # Videos are checked by filename
                 log_reason = "filename match"
                 exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type)
            elif not is_video: # Standard image
                 # Images are checked by MD5 hash
                 log_reason = "MD5 hash match"
                 if current_md5_hash:
                      exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type, file_hash=current_md5_hash)
                 else:
                      # This should not happen if hashing logic above is correct, but check defensively
                      logger.error(f"{log_identifier}: Cannot check SmugMug for image duplicate, MD5 hash missing unexpectedly.")
                      db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, "MD5 missing before SM duplicate check")
                      # Cleanup temp file if it exists
                      if temp_file_path and os.path.exists(temp_file_path):
                         # Use proper try/except block structure
                         try:
                             os.remove(temp_file_path)
                             logger.debug(f"{log_identifier}: Cleaned temp file after missing hash error.")
                         except OSError as e:
                             logger.warning(f"{log_identifier}: Failed to clean temp file {temp_file_path} after missing hash error: {e}")
                      return google_id, STATUS_ERROR_HASHING # Exit processing

            # Process duplicate check result
            if exists_on_smugmug:
                 # Determine duplicate status based on check method
                 duplicate_status = STATUS_DUPLICATE_FILENAME if is_video else STATUS_DUPLICATE_HASH
                 logger.info(f"{log_identifier}: Found on SmugMug ({log_reason}). Marking as duplicate.")
                 db_manager.update_item_status(google_id, duplicate_status, error_message=f"Duplicate check via {log_reason}")
                 final_status = duplicate_status
                 # Optional: Log simulated deletion from Google Photos if flag is set
                 if args.delete_from_google:
                     # Note: remove_photo currently only simulates deletion
                     google_photos.remove_photo(google_id, dry_run=args.dry_run)
                 # Cleanup temp file if it exists (might not exist for videos if check passed before download)
                 if temp_file_path and os.path.exists(temp_file_path):
                      try:
                          os.remove(temp_file_path)
                          logger.debug(f"{log_identifier}: Cleaned temp file for duplicate item.")
                      except OSError as e:
                          logger.warning(f"{log_identifier}: Failed clean temp file for duplicate item: {e}")
                 return google_id, final_status # Stop processing this item - it's a duplicate
            else:
                 # Only log if not found (reduces noise vs logging every check start)
                 logger.info(f"{log_identifier}: Checked SmugMug via {log_reason}: Not found.")
                 # Update DB status to indicate check completed and item needs upload
                 db_manager.update_item_status(google_id, STATUS_SMUGMUG_CHECKED_NOT_FOUND, error_message=f"SM check via {log_reason} - not found")
                 final_status = STATUS_SMUGMUG_CHECKED_NOT_FOUND # Update status before potential upload

        # --- Upload Step (Conditional) ---
        # Proceed only if not skipped, not duplicate, and no errors occurred so far

        # Handle Dry Run: If dry run is enabled, simulate upload and cleanup
        if args.dry_run:
            # Only log dry-run message if the item would have been uploaded
            if final_status not in [STATUS_SKIPPED_FILTER, STATUS_SKIPPED_HEIC, STATUS_DUPLICATE_FILENAME, STATUS_DUPLICATE_HASH] and \
               final_status not in ERROR_STATUSES:
                logger.info(f"{log_identifier}: [DRY RUN] Would upload.")
                # Keep status as checked for dry run, don't mark as uploaded
                final_status = STATUS_SMUGMUG_CHECKED_NOT_FOUND
                # Simulate Google Photos deletion if flag is set
                if args.delete_from_google:
                    google_photos.remove_photo(google_id, dry_run=True)
                # Cleanup temp file manually in dry run if it exists
                if temp_file_path and os.path.exists(temp_file_path):
                     try:
                         os.remove(temp_file_path)
                         logger.debug(f"{log_identifier}: [DRY RUN] Cleaned temp file.")
                     except OSError as e:
                         logger.warning(f"{log_identifier}: [DRY RUN] Failed clean temp file: {e}")
            # Return whatever the status was before the dry-run check
            return google_id, final_status

        # --- Proceed with Actual Upload ---
        # Ensure temp file exists before attempting upload (might need re-download)
        if not temp_file_path or not os.path.exists(temp_file_path):
            # This implies download was needed but failed, or file disappeared.
            # Attempt re-download only if download was originally needed for this item type.
            if needs_download:
                 logger.warning(f"{log_identifier}: Temp file path missing before upload. Attempting download again...")
                 # Reuse original item_details for re-download attempt
                 temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details)
                 # Check if re-download succeeded
                 if not temp_file_path:
                      logger.error(f"{log_identifier}: Re-download failed before upload.")
                      db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, "Re-download failed before upload attempt")
                      return google_id, STATUS_ERROR_DOWNLOAD # Exit processing
                 # Update DB if details refreshed during re-download
                 if refreshed_details:
                      new_base_url = refreshed_details.get('baseUrl')
                      new_metadata = refreshed_details.get('mediaMetadata')
                      new_metadata_json = json.dumps(new_metadata) if new_metadata else item_details.get('media_metadata_json')
                      db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
            else:
                # If download wasn't originally needed, but file is missing now, it's an unexpected error
                 logger.error(f"{log_identifier}: Temp file path missing unexpectedly before upload. Skipping. Expected path: {temp_file_path}")
                 db_manager.update_item_status(google_id, STATUS_ERROR_UNKNOWN, "Temp file missing unexpectedly before upload")
                 return google_id, STATUS_ERROR_UNKNOWN # Exit processing


        # --- Upload Attempt ---
        # Use the confirmed album API URI from the initialized SmugMug object
        target_album_uri_for_upload = smugmug.album_api_uri
        if not target_album_uri_for_upload:
             # Should not happen if initialization succeeded, but check defensively
             logger.error(f"{log_identifier}: SmugMug target album URI missing. Cannot upload.")
             db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, "SM album URI missing during upload")
             # Cleanup temp file if it exists
             if temp_file_path and os.path.exists(temp_file_path):
                 # Use proper try/except block structure
                 try:
                     os.remove(temp_file_path)
                     logger.debug(f"{log_identifier}: Cleaned temp file after SM API error (missing URI).")
                 except OSError as e:
                     logger.warning(f"{log_identifier}: Failed to clean temp file {temp_file_path} after SM API error: {e}")
             return google_id, STATUS_ERROR_SMUGMUG_API # Exit processing

        # Log upload attempt and update DB status
        logger.info(f"{log_identifier}: Uploading to SmugMug album URI: {target_album_uri_for_upload}...")
        db_manager.update_item_status(google_id, STATUS_UPLOAD_ATTEMPTED, increment_attempt=True)
        final_status = STATUS_UPLOAD_ATTEMPTED # Update intermediate status

        # Call the upload method - it handles temp file cleanup internally
        upload_success = smugmug.upload_media(target_album_uri_for_upload, temp_file_path, filename, mime_type)

        # Process upload result
        if upload_success:
            logger.info(f"{log_identifier}: Upload successful.")
            db_manager.update_item_status(google_id, STATUS_UPLOADED_SUCCESS)
            final_status = STATUS_UPLOADED_SUCCESS
            # Optional: Log simulated deletion from Google Photos if flag is set
            if args.delete_from_google:
                # Note: remove_photo currently only simulates deletion
                google_photos.remove_photo(google_id, dry_run=False)
        else:
            # upload_media logs the specific upload error
            logger.error(f"{log_identifier}: Upload failed.")
            # Update DB status to reflect upload failure
            db_manager.update_item_status(google_id, STATUS_ERROR_UPLOAD_FAILED, "Upload function returned failure")
            final_status = STATUS_ERROR_UPLOAD_FAILED

        # Temp file path should be None now as upload_media cleans it up
        temp_file_path = None
        return google_id, final_status # Return final status after upload attempt

    except Exception as e:
        # --- Catch-all for Unexpected Errors in Worker ---
        logger.error(f"Unexpected exception in processing worker for {google_id} ('{filename}'): {e}\n{traceback.format_exc()}")
        final_status = STATUS_ERROR_UNKNOWN # Mark as unknown error
        # Attempt to update DB status with the error, but might fail
        try:
             # Include part of the exception message in the DB error field
             error_msg_short = str(e)[:200] # Limit error message length for DB
             db_manager.update_item_status(google_id, final_status, f"Worker exception: {error_msg_short}")
        except Exception as db_e:
             # Log secondary error if DB update fails
             logger.error(f"Failed to update DB status after worker exception for {google_id}: {db_e}")
        # Ensure cleanup of temp file if it still exists due to early exit or error
        if temp_file_path and os.path.exists(temp_file_path):
             # Use proper try/except block structure
             try:
                 os.remove(temp_file_path)
                 logger.debug(f"{log_identifier}: Cleaned temp file after worker exception.")
             except OSError as clean_e:
                 logger.warning(f"{log_identifier}: Failed clean temp file {temp_file_path} after worker exception: {clean_e}")
        # Return the error status
        return google_id, final_status


# --- Main Function ---
def main():
    """Main execution function: parses args, initializes modules, runs processing loop."""
    global logger, shutdown_requested, google_photos_instance_global, db_manager_global, shutdown_event, PROGRESS_LEVEL_NUM
    # Access global counters for summary
    global uploaded_in_run, duplicates_in_run, skipped_in_run, errors_in_run

    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(
        description="Transfer Google Photos to SmugMug using SQLite and parallel workers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter # Provides default values in help message
    )
    # Define command-line arguments (same as before)
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
    parser.add_argument('--retry-errors', action='store_true', help='Include items currently marked with an error status in this processing run.')
    parser.add_argument('--reset-errors', action='store_true', help='Reset all items currently marked with an error status back to PENDING before starting the run.')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS, help=f'Number of parallel worker threads for processing items (default: {MAX_WORKERS}).')

    # Parse the arguments provided by the user
    args = parser.parse_args()

    # Validate and set the number of workers
    num_workers = args.workers
    if num_workers <= 0:
         # Logger might not be setup yet, print warning to stderr
         print(f"Warning: Number of workers must be positive. Using default: {MAX_WORKERS}", file=sys.stderr)
         num_workers = MAX_WORKERS

    # --- Setup Logging ---
    setup_logging(args.debug) # Configure logger based on debug flag

    # --- Acquire Lock File ---
    if not acquire_lock():
        # Exit if lock file exists or cannot be created
        sys.exit(1)

    # --- Setup Signal Handling ---
    # Register signal handlers for graceful shutdown on common termination signals
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler) # kill command
    if hasattr(signal, 'SIGBREAK'): # Windows specific signal (Ctrl+Break)
        signal.signal(signal.SIGBREAK, signal_handler)

    # --- Initialize Variables ---
    smugmug = None
    google_photos = None
    db_manager = None
    cleanup_handled_in_try = False # Flag to track if cleanup occurred successfully within try block
    start_time = time.time() # Record start time for duration calculation
    # Reset run summary counters at the beginning of each run
    processed_count_this_run = 0 # Local counter for items processed in the loop
    uploaded_in_run = 0
    duplicates_in_run = 0
    skipped_in_run = 0
    errors_in_run = 0
    total_db_items = 0 # Initialize total DB items
    total_items_to_process_this_run = 0 # Will be updated after fetching item list

    # --- Main Execution Block (Try/Except/Finally) ---
    try:
        logger.info(f"--- Starting gp2sm v{__version__} ---")
        logger.info(f"Using database file: {args.db_file}")
        logger.info(f"Parallel Workers Enabled: Workers={num_workers}")
        logger.info(f"Command line arguments: {vars(args)}")
        # Log initialization start
        logger.info("Initializing modules...")

        # --- Initialize Database Manager ---
        try:
             db_manager = DatabaseManager(db_file=args.db_file)
             db_manager_global = db_manager # Store globally for cleanup
             logger.info("DB manager initialized.")
        except Exception as e:
             # Log critical error and exit if DB initialization fails
             logger.critical(f"Failed init DB manager: {e}", exc_info=True)
             # Use print as logger might be partially configured
             print(f"Critical Error: Failed to initialize database manager: {e}", file=sys.stderr)
             sys.exit(1)

        # --- Initialize SmugMug Module & Authenticate ---
        try:
            # Create SmugMug instance and load config
            smugmug = SmugMug(config_file='smugmug_config.json')
            smugmug.load_config()
        except FileNotFoundError:
             # Handle missing config file: generate default and exit
             logger.warning(f"SmugMug config file '{smugmug.config_file}' missing.")
             # Use print as logger might be partially configured
             print(f"\nWarning: SmugMug config file '{smugmug.config_file}' not found.", file=sys.stderr)
             if smugmug.generate_default_config():
                 # Exit after generating template, user needs to edit it
                 sys.exit(0)
             else:
                 # Error generating default config
                 logger.critical("Failed generate default SmugMug config.")
                 print("Critical Error: Failed to generate default SmugMug config.", file=sys.stderr)
                 sys.exit(1)
        except Exception as e:
             # Handle other errors during config loading
             logger.critical(f"Failed load SmugMug config: {e}", exc_info=True)
             print(f"Critical Error: Failed to load SmugMug config: {e}", file=sys.stderr)
             sys.exit(1)

        # Apply command-line overrides to SmugMug config *before* authentication/album check
        current_smugmug_album_arg = args.smugmug_album
        current_smugmug_folder_arg = args.smugmug_folder
        current_google_album_arg = args.google_photos_album_id # Store Google Album ID arg

        if args.process_heic:
            if smugmug.config:
                smugmug.config['process_heic'] = True
                logger.info("Override: HEIC processing enabled via command line.")
            else:
                # Should not happen if load_config succeeded, but check defensively
                logger.error("Cannot apply HEIC override: SmugMug config not loaded.")
                sys.exit(1) # Exit if config is unexpectedly None
        if current_smugmug_album_arg:
             logger.info(f"Override: Using SM Album Name from command line: '{current_smugmug_album_arg}'")
             if smugmug.config:
                  smugmug.config['album_name'] = current_smugmug_album_arg
                  # Reset key/uri in config if name is provided via args,
                  # so get_or_create_album_in_path prioritizes the name.
                  smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']
                  smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
             else:
                 logger.error("Cannot apply album override: SmugMug config not loaded.")
                 sys.exit(1) # Exit if config is unexpectedly None
        if current_smugmug_folder_arg:
             logger.info(f"Override: Using SM Folder Name from command line: '{current_smugmug_folder_arg}'")
             if smugmug.config:
                 smugmug.config['folder_name'] = current_smugmug_folder_arg
             else:
                 logger.error("Cannot apply folder override: SmugMug config not loaded.")
                 sys.exit(1) # Exit if config is unexpectedly None
        elif 'folder_name' not in smugmug.config:
              # Ensure folder_name key exists in config, even if None/empty, for consistency
              smugmug.config['folder_name'] = None

        # Authenticate with SmugMug and check configuration consistency
        logger.info("Authenticating with SmugMug and checking configuration...")
        if not smugmug.check_config_and_authenticate():
            # Exit if authentication or config check fails
            logger.critical("SmugMug auth/config check failed.")
            # Use print as logger might be partially configured or error is critical
            print("Critical Error: SmugMug authentication or configuration failed. Please check config file and logs.", file=sys.stderr)
            sys.exit(1)
        logger.info("SmugMug init and auth successful.")

        # --- Ensure Target SmugMug Album Exists ---
        # Get final target album/folder names from the config object after potential overrides
        current_target_album_name = smugmug.config.get('album_name')
        current_target_folder_path = smugmug.config.get('folder_name') # This might be None

        logger.info("Ensuring target SmugMug album exists...")
        # This function handles finding/creating folders and the album itself.
        # It updates smugmug.album_key and smugmug.album_api_uri upon success.
        if not smugmug.get_or_create_album_in_path(current_target_album_name, current_target_folder_path):
             # Exit if target album cannot be found or created
             logger.critical("Failed find/create target SmugMug album.")
             print(f"Critical Error: Failed to find or create SmugMug album '{current_target_album_name or '(using key)'}' in folder '{current_target_folder_path or 'root'}'.", file=sys.stderr)
             sys.exit(1)

        # Store the confirmed target album key/uri after get_or_create
        current_target_album_key = smugmug.album_key
        current_target_album_uri = smugmug.album_api_uri
        logger.info(f"Confirmed Target - SM Key: {current_target_album_key}, URI: {current_target_album_uri}")

        # --- Initialize Google Photos Module & Authenticate ---
        try:
            logger.info("Initializing Google Photos module...")
            google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json')
            google_photos_instance_global = google_photos # Store globally for cleanup
            # Initial authentication happens within GooglePhotos init or first API call
            # Perform a check to ensure it's valid before proceeding
            if not google_photos.is_authenticated():
                 logger.critical("Google Photos module failed initial authentication check.")
                 print("Critical Error: Google Photos authentication failed. Check token/credentials.", file=sys.stderr)
                 sys.exit(1)
            logger.info("Google Photos init and auth successful.")
        except GoogleCredentialsNotFoundError as e:
            # Handle missing Google credentials file specifically
            # Use print as logger setup might depend on successful imports
            print(f"\nError: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            # Handle other Google Photos initialization errors
            logger.critical(f"Failed init Google Photos module: {e}", exc_info=True)
            print(f"Critical Error: Failed to initialize Google Photos module: {e}", file=sys.stderr)
            sys.exit(1)


        # --- Database Population / Config Consistency Check ---
        is_initial_run = False # Flag to track if this is the very first run with this DB
        total_db_items = db_manager.get_item_count() # Get current item count *before* potential fetch
        stored_config = db_manager.get_stored_config() # Get config snapshot from DB

        # Determine if a fresh list fetch from Google Photos is needed
        needs_fetch = (total_db_items == 0) or args.force_refresh_list

        if needs_fetch:
            # Mark as initial run only if the DB was actually empty before fetch
            is_initial_run = (total_db_items == 0)
            if args.force_refresh_list:
                logger.warning("Force refresh requested: Re-fetching media list from Google Photos...")
            else: # DB was empty
                logger.info("Database empty. Fetching initial list from Google Photos...")

            # Ensure Google Photos token is valid before making API call
            if not google_photos.refresh_token_if_needed():
                if not google_photos.authenticate(): # Try full re-authentication if refresh fails
                    logger.critical("Google Photos re-authentication failed before fetching list.")
                    print("Critical Error: Google Photos authentication failed.", file=sys.stderr)
                    sys.exit(1)

            # Fetch the list of media items from Google Photos API
            logger.info(f"Fetching media list from Google Photos source: {current_google_album_arg or 'Entire Library'}...")
            photos_list = google_photos.get_photos(current_google_album_arg)
            logger.info(f"Fetched {len(photos_list)} items from Google Photos.")

            # If forcing refresh on an existing DB, clear old entries first
            if args.force_refresh_list and not is_initial_run:
                 logger.warning("Force refresh: Clearing existing items from media table before adding new list.")
                 try:
                     # Use a context manager for the database operation
                     with db_manager.conn:
                         db_manager.conn.execute(f"DELETE FROM {MEDIA_TABLE_NAME}")
                     logger.info("Cleared existing media items due to force refresh.")
                 except Exception as del_e:
                     # Log error but proceed cautiously if clearing fails
                     logger.error(f"Failed to clear existing items during force refresh: {del_e}", exc_info=True)
                     # Use print as logger might be involved in the error
                     print(f"Warning: Failed to clear old items from DB during force refresh: {del_e}", file=sys.stderr)

            # Save the current run configuration snapshot to the DB
            # This happens on initial run or after clearing for force refresh
            if is_initial_run or args.force_refresh_list:
                # Use the folder name confirmed by get_or_create_album_in_path
                if not db_manager.save_initial_config(current_target_album_key, current_target_album_uri, smugmug.folder_name, current_google_album_arg):
                     logger.error("Failed save initial config snapshot to database.")
                     # Non-critical, proceed but log error

            # Add the fetched items to the database
            if photos_list:
                 logger.info(f"Adding {len(photos_list)} fetched items to the database...")
                 added_count = db_manager.add_item_batch(photos_list, current_target_album_key)
                 logger.info(f"Populated DB with {added_count} new items.")
                 total_db_items = db_manager.get_item_count() # Update total count *after* adding
            else:
                 # No items found in Google Photos source
                 logger.info("No items found in Google Photos source to add to DB.")
                 # If this was the initial run and fetch returned nothing, exit gracefully.
                 if is_initial_run:
                      logger.info("Exiting as no items were found in the source on initial run.")
                      sys.exit(0)
                 # If force_refresh resulted in no items, continue (DB is now empty or cleared)
                 total_db_items = 0 # Update total count if DB was cleared/empty

        else:
             # Database exists and not forcing refresh, check config consistency
             logger.info(f"DB contains {total_db_items} items. Checking config consistency...")
             if stored_config:
                # Compare stored config with current settings
                stored_sm_key = stored_config.get('smugmug_album_key')
                stored_sm_folder = stored_config.get('smugmug_folder_name')
                stored_gp_album = stored_config.get('google_album_id')
                # Use folder_name from the authenticated smugmug object for current state
                current_folder_norm = smugmug.folder_name if smugmug.folder_name else None
                stored_folder_norm = stored_sm_folder if stored_sm_folder else None
                current_gp_album_norm = current_google_album_arg if current_google_album_arg else None
                stored_gp_album_norm = stored_gp_album if stored_gp_album else None
                mismatch = False
                # Check each relevant configuration parameter for mismatch
                if current_target_album_key != stored_sm_key:
                    logger.warning(f"Config Mismatch: SmugMug Album Key! Current='{current_target_album_key}', Stored='{stored_sm_key}'")
                    mismatch = True
                if current_folder_norm != stored_folder_norm:
                    logger.warning(f"Config Mismatch: SmugMug Folder! Current='{current_folder_norm or 'Root'}', Stored='{stored_folder_norm or 'Root'}'")
                    mismatch = True
                if current_gp_album_norm != stored_gp_album_norm:
                    logger.warning(f"Config Mismatch: Google Album ID! Current='{current_gp_album_norm or 'Library'}', Stored='{stored_gp_album_norm or 'Library'}'")
                    mismatch = True

                # If any mismatch found, warn user and use stored settings
                if mismatch:
                    # Log multi-line warning
                    logger.warning("="*60)
                    logger.warning("WARNING: CONFIGURATION MISMATCH DETECTED!")
                    logger.warning(">>> CONTINUING WITH STORED SETTINGS FROM DATABASE <<<")
                    logger.warning(f"    SmugMug Album Key: {stored_sm_key}")
                    logger.warning(f"    SmugMug Folder:    {stored_folder_norm or 'Root'}")
                    logger.warning(f"    Google Album ID:   {stored_gp_album_norm or 'Entire Library'}")
                    logger.warning("\nTo use NEW settings, stop (Ctrl+C), delete the database file")
                    logger.warning(f"('{args.db_file}') or use --force-refresh-list.")
                    logger.warning("="*60)
                    time.sleep(5) # Pause to ensure user sees the message in logs
                    # Apply stored settings to the SmugMug object for the rest of the run
                    smugmug.album_key = stored_sm_key
                    smugmug.album_api_uri = stored_config.get('smugmug_album_uri')
                    smugmug.folder_name = stored_folder_norm
                    smugmug.album_name = None # Clear name as we are using key/uri from stored config
                    current_target_album_key = stored_sm_key # Update 'current' var to reflect stored value being used
                else:
                    # Config matches, proceed normally
                    logger.info("Config matches snapshot stored in database.")
             else:
                # DB exists but no config snapshot found (less common case)
                logger.warning("Existing DB found, but no config snapshot. Saving current config and proceeding.")
                # Save the current configuration as the snapshot
                if not db_manager.save_initial_config(current_target_album_key, current_target_album_uri, smugmug.folder_name, current_google_album_arg):
                     logger.error("Failed save current config snapshot to existing DB.")
                     # Non-critical, proceed but log error


        # --- Reset Errored Items (Optional) ---
        if args.reset_errors:
            logger.info("Resetting items with error status back to PENDING...")
            reset_count = db_manager.reset_failed_items()
            logger.info(f"Reset {reset_count} items.")
            total_db_items = db_manager.get_item_count() # Re-fetch total count after reset

        # --- Get Items for Processing ---
        logger.info("Fetching items to process from database...")
        # Get list of items based on status (includes errors if --retry-errors is used)
        items_to_process_list = db_manager.get_items_to_process(retry_errors=args.retry_errors)
        total_items_to_process_this_run = len(items_to_process_list) # Total items for this specific run
        # Get total items again *after* potential reset/fetch to use for overall progress calc
        total_db_items = db_manager.get_item_count()
        logger.info(f"Found {total_items_to_process_this_run} items requiring processing out of {total_db_items} total items in DB.")

        # Check if there's anything to process
        if total_items_to_process_this_run == 0:
             if total_db_items > 0:
                 logger.info("No items require processing in this run (all items in terminal/skipped state).")
             else:
                 logger.info("No items found in database to process.")
             # Skip the processing loop and go directly to summary
        else:
            # Items found, log message and proceed to processing loop
            logger.info(f"Starting {num_workers} workers to process {total_items_to_process_this_run} items...")


        # --- Parallel Processing Loop ---
        # Use ThreadPoolExecutor to manage worker threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix='Worker') as executor:
            submitted_futures = [] # List to hold future objects for submitted tasks
            num_submitted = 0 # Counter for tasks actually submitted

            # Submit tasks only if there are items to process
            if total_items_to_process_this_run > 0:
                logger.info(f"Submitting {total_items_to_process_this_run} items to workers...")
                # Iterate through items and submit each to the executor
                for item_details in items_to_process_list:
                    # Check for shutdown signal before submitting each task
                    if shutdown_event.is_set():
                        logger.warning("Shutdown signalled during task submission. Stopping submission.")
                        break # Stop submitting new tasks
                    # Submit the worker function with necessary arguments
                    future = executor.submit(process_item_worker, item_details, google_photos, smugmug, db_manager, args)
                    submitted_futures.append(future)
                    num_submitted += 1 # Increment count of submitted tasks

                logger.info(f"Submitted {num_submitted} tasks. Waiting for completion...")

                # Process results as futures complete
                processed_count_this_run = 0 # Reset local counter for completed items in this run
                # Iterate over futures as they complete (order is not guaranteed)
                # Ensure we only iterate over the futures that were actually submitted
                active_futures = submitted_futures[:num_submitted]
                for future in concurrent.futures.as_completed(active_futures):
                    processed_count_this_run += 1 # Increment as soon as a future completes
                    try:
                        # Retrieve the result from the completed future (google_id, final_status)
                        google_id, final_status = future.result()

                        # Update global summary counters based on the returned status
                        if final_status == STATUS_UPLOADED_SUCCESS:
                            uploaded_in_run += 1
                        elif final_status in [STATUS_DUPLICATE_FILENAME, STATUS_DUPLICATE_HASH]:
                            duplicates_in_run += 1
                        elif final_status in [STATUS_SKIPPED_FILTER, STATUS_SKIPPED_HEIC]:
                            skipped_in_run += 1
                        elif final_status in ERROR_STATUSES:
                            errors_in_run += 1

                        # --- Log Progress Update (Overall) ---
                        # Calculate overall progress based on total DB items
                        items_remaining_to_process = total_items_to_process_this_run - processed_count_this_run
                        overall_processed_count = total_db_items - items_remaining_to_process
                        percentage = (overall_processed_count / total_db_items) * 100 if total_db_items > 0 else 0
                        last_id_short = f"{google_id[:8]}..." if google_id else "N/A"

                        # Log the progress information using the custom PROGRESS level
                        logger.progress(
                            f"Overall Progress: {overall_processed_count}/{total_db_items} ({percentage:.1f}%) "
                            f"| Run: Up: {uploaded_in_run} Dup: {duplicates_in_run} Skip: {skipped_in_run} Err: {errors_in_run} "
                            f"| Last: {final_status} (ID: {last_id_short})"
                        )

                    except Exception as exc:
                        # Catch exceptions that might occur during future.result() or if worker raised unhandled exception
                        logger.error(f"Exception retrieving result from worker future: {exc}", exc_info=True)
                        errors_in_run += 1 # Count this as an error for the summary

                        # Log progress even on error retrieving result
                        items_remaining_to_process = total_items_to_process_this_run - processed_count_this_run
                        overall_processed_count = total_db_items - items_remaining_to_process
                        percentage = (overall_processed_count / total_db_items) * 100 if total_db_items > 0 else 0
                        logger.error(
                            f"Overall Progress: {overall_processed_count}/{total_db_items} ({percentage:.1f}%) "
                            f"| Run: Up: {uploaded_in_run} Dup: {duplicates_in_run} Skip: {skipped_in_run} Err: {errors_in_run} "
                            f"| Last: ERROR retrieving future result"
                        )

                    # Check if main thread should break due to shutdown signal
                    if shutdown_event.is_set():
                        logger.warning("Shutdown requested. Breaking from processing completed futures loop.")
                        break # Exit the as_completed loop

                # --- End of processing loop ---
                logger.info("Worker processing loop finished or interrupted.")

            # --- Final Run Summary ---
            total_duration = time.time() - start_time # Calculate total run duration
            # Log detailed summary
            logger.info("=" * 60)
            logger.info("Run Summary:")
            logger.info(f"  Items Submitted This Run:      {num_submitted}")
            logger.info(f"  Items Processed (Completed):   {processed_count_this_run}")
            logger.info(f"  Uploaded this Run:           {uploaded_in_run}")
            logger.info(f"  Marked as Duplicate this Run: {duplicates_in_run}")
            logger.info(f"  Skipped this Run:            {skipped_in_run}")
            logger.info(f"  Errors this Run:             {errors_in_run}")
            logger.info(f"  Total processing time:       {total_duration:.2f} seconds")
            logger.info("-" * 60)
            logger.info("Overall Database Stats:")
            # Get final stats from the database
            final_stats = db_manager.get_stats() if db_manager else {}
            # Log count for each status
            for status, count in sorted(final_stats.items()):
                logger.info(f"  - {status}: {count}")
            logger.info("=" * 60)

            # --- Log Concise Summary ---
            # This provides a quick overview in the logs
            logger.info("--- Concise Run Summary ---")
            logger.info(f"- Items Submitted: {num_submitted}")
            logger.info(f"- Items Processed: {processed_count_this_run}")
            logger.info(f"- Uploaded{' (Dry Run)' if args.dry_run else ''}:      {uploaded_in_run}")
            logger.info(f"- Duplicates Found: {duplicates_in_run}")
            logger.info(f"- Skipped:          {skipped_in_run}")
            logger.info(f"- Errors:           {errors_in_run}")

            # --- Determine Final Status and Handle DB Rename ---
            final_db_filename = args.db_file # Default DB filename
            # Run considered fully complete if no errors occurred AND shutdown wasn't requested
            run_completed_successfully = (errors_in_run == 0 and not shutdown_requested)

            if run_completed_successfully:
                logger.info("- Status: Completed run without errors")
                # Check if any non-terminal items remain in the DB
                remaining_items = db_manager.get_items_to_process(retry_errors=False) if db_manager else []
                if not remaining_items:
                    # All items processed successfully, attempt to rename DB
                    logger.info("All targeted items processed successfully. Run complete.")
                    logger.info("Attempting to rename completed database file...")
                    # Ensure DB connection is closed before renaming
                    if db_manager_global:
                        db_manager_global.close(); logger.info("Closed DB connection before renaming.")
                        db_manager_global = None # Clear global ref as it's closed
                    else:
                        logger.warning("DB manager instance not found for closing before rename.")

                    # Construct new filename with timestamp
                    base_db_name, db_ext = os.path.splitext(args.db_file)
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    new_db_filename = f"{base_db_name}_completed_{timestamp}{db_ext}"
                    try:
                        # Check if original DB file exists before renaming
                        if os.path.exists(args.db_file):
                            os.rename(args.db_file, new_db_filename)
                            logger.info(f"Successfully renamed database to: {new_db_filename}")
                            final_db_filename = new_db_filename # Update filename for final message
                            cleanup_handled_in_try = True # Flag that cleanup (DB close, lock release) happened here
                            release_lock() # Release lock manually as normal cleanup will be skipped
                        else:
                            # Original DB file might have been closed/moved already
                            logger.warning(f"DB file {args.db_file} not found for renaming (perhaps already closed/moved?).")
                    except OSError as e:
                        # Handle rename error
                        logger.error(f"Failed to rename database file: {e}", exc_info=True)
                        # Use print for this critical error message as well
                        print(f"\nERROR: Failed to rename completed database file: {e}", file=sys.stderr)
                else:
                     # Run completed without errors, but items remain
                     logger.warning(f"Run completed without errors, but {len(remaining_items)} items still require processing. DB not renamed.")
            elif shutdown_requested:
                # Run was terminated by user signal
                logger.warning("- Status: Terminated by user")
            else:
                # Run completed, but errors occurred
                logger.error(f"- Status: Completed run with {errors_in_run} errors")

            # Log final stats and info
            logger.info(f"- Run Time: {total_duration:.2f} sec")
            logger.info("-" * 60)
            logger.info("Overall Database Stats (Final):")
            # Log non-zero counts for each status
            for status, count in sorted(final_stats.items()):
                 if count > 0:
                     logger.info(f"- {status}: {count}")
            logger.info(f"- Detailed Log File: {LOG_FILE}")
            logger.info(f"- Database File: {final_db_filename}") # Show final DB filename
            logger.info("=" * 60)
            # --- End of Summary Block ---

        # --- End of 'with concurrent.futures.ThreadPoolExecutor' block ---
        # Executor automatically shuts down here, waiting for running threads if needed

    # --- Exception Handling for Main Block ---
    except KeyboardInterrupt:
        # Handle Ctrl+C specifically if it wasn't caught by signal handler earlier
        if not shutdown_requested:
             # Use logger if available, otherwise print
             log_func = getattr(logger, 'warning', lambda msg: print(msg, file=sys.stderr))
             log_func("Keyboard interrupt detected directly in main execution block.")
             log_func("Shutting down...")
             shutdown_event.set() # Signal threads to stop
             shutdown_requested = True
    except Exception as e:
        # Catch any other unexpected critical errors during main execution
        # Use logger if available, otherwise print
        log_func = getattr(logger, 'critical', lambda msg, exc_info: print(msg, file=sys.stderr))
        log_func(f"Critical unexpected error in main execution: {e}", exc_info=True)
        # Also print a minimal error message to stderr
        print(f"\nCritical Error: {e}. Check log '{LOG_FILE}'.", file=sys.stderr)
        shutdown_event.set() # Signal threads to stop on critical error
        shutdown_requested = True
    # --- End of main try block's except clauses ---

    # --- Finally Block: Ensures Cleanup Runs ---
    finally:
        # Use logger if available, otherwise print
        log_func_info = getattr(logger, 'info', lambda msg: print(f"INFO: {msg}", file=sys.stderr))
        log_func_info("Executing final cleanup...")
        # Ensure shutdown event is set, regardless of how the try block exited
        shutdown_event.set()
        # Perform cleanup (close DB, remove temp files, release lock)
        # only if it wasn't already handled successfully during DB rename
        if not cleanup_handled_in_try:
            cleanup(google_photos_instance_global, db_manager_global)
        else:
            # Log that normal cleanup is skipped because DB rename handled it
            log_func_info("Skipping normal cleanup as DB was successfully renamed (implies lock release and DB close).")

        # Determine final exit code: 1 if errors occurred or was shut down, 0 otherwise
        exit_code = 1 if errors_in_run > 0 or shutdown_requested else 0
        log_func_info(f"Exiting script with code {exit_code}.")
        # Ensure all buffered log messages are flushed before exiting
        logging.shutdown()
        sys.exit(exit_code)

# --- Script Entry Point ---
if __name__ == "__main__":
    # Call the main function when the script is executed directly
    main()
