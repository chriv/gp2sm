# Google Photos to SmugMug Transfer Script
#
# This script facilitates transferring media from Google Photos to SmugMug.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#
__version__ = "1.6"  # Version includes signal handling, logging improvements

# Standard library imports
import argparse
import json                 # Needed for JSON checks
import logging
import os
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler
import signal # Added for signal handling

# Third-party imports
import colorlog

# Local module imports
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug, DEFAULT_SMUGMUG_CONFIG # Import default config

# --- TODO List ---
# DONE: Clean up temp_downloads (handled by google_photos_module.__del__) -> Now explicit cleanup
# DONE: Provide more output when handling batches of Google Photos files (added progress indicators)
# DONE: Ignore HEIC files by default (override in config and/or command line)
# DONE: Warn users about HEIC files being converted to JPEGS (and flattened) by SmugMug
# DONE: Don't duplicate check HEIC files at all if being processed (no possible way)
# DONE: (Deferred - Not Feasible) Get MD5 hashes from Google Photos API before downloading.
# DONE: Generate a smugmug_config.json if missing (with instructions)
# DONE: Add Folder creation support
# DONE: Check SmugMug config for missing/placeholder API keys/secrets and Album config
# DONE: Check google_api_keys.json existence and provide instructions
# DONE: Add Command-Line Arguments (Google Album ID, Simulated Delete, SmugMug Album/Folder override, Dry Run, Ignore Photos/Videos, Process HEIC, Debug, Version)
# DONE: Implement Color Logging
# DONE: Add Lock File
# DONE: Add Log Rotation
# DONE: Handle Google API Token expiry/refresh proactively
# DONE: Handle Google API download URL expiry (401/403) with item re-fetch and retry
# DONE: Handle SmugMug attribute validation/correction (WorldSearchable, etc.)
# IN PROGRESS v1.6: Implement graceful shutdown via signal handling
# IN PROGRESS v1.6: Refactor logging setup
# IN PROGRESS v1.6: Enhance console colors
# IN PROGRESS v1.6: Refactor cleanup logic
# TODO: Add support for Google Photos "Shared Albums" (might require different API scope/logic)
# TODO: Investigate parallel uploads/downloads? (Complexity vs benefit)
# TODO: Option to specify start/end date range for Google Photos fetch?
# TODO: Option to specify number of items to process?
# TODO: Better error reporting summary (e.g., list specific failed files?)

# --- Constants ---
LOG_FILE = "gp2sm_transfer.log"
LOCK_FILE = "gp2sm.lock"
TOKEN_REFRESH_CHECK_INTERVAL = 100 # Check Google token expiry roughly every 100 items

# --- Global Variables ---
logger = None # Logger will be configured in setup_logging
shutdown_requested = False # Flag for graceful shutdown

# --- Signal Handling (v1.6) ---
def signal_handler(sig, frame):
    """Handles termination signals for graceful shutdown."""
    global shutdown_requested, logger
    if not shutdown_requested: # Prevent multiple shutdown messages
        try:
             signal_name = signal.Signals(sig).name
        except ValueError:
             signal_name = f"Signal {sig}"

        # Use logger if available, otherwise print
        log_func = logger.warning if logger else lambda msg: print(f"WARNING: {msg}")
        print_func = print

        log_func(f"Received signal {signal_name}. Initiating graceful shutdown...")
        print_func(f"\nSignal {signal_name} received. Cleaning up...")
        shutdown_requested = True
    else:
        if logger: logger.debug(f"Shutdown already requested. Received signal {signal_name} ({sig}) again.")

# --- Logging Setup (v1.6 Refactor) ---
def setup_logging(debug=False):
    """Configures console and file logging."""
    global logger
    log_level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger() # Get root logger
    logger.setLevel(log_level) # Set the minimum level for the logger itself

    # Prevent adding handlers multiple times if setup_logging is called again
    if logger.hasHandlers():
        logger.handlers.clear()

    # Console Handler (Colorized)
    console_format = (
        '%(asctime)s - '
        '%(log_color)s%(levelname)-8s%(reset)s - '
        '[%(name)s:%(funcName)s:%(lineno)d] - '
        '%(message_log_color)s%(message)s%(reset)s' # Added message_log_color
    )
    console_formatter = colorlog.ColoredFormatter(
        console_format,
        datefmt='%Y-%m-%d %H:%M:%S',
        reset=True,
        log_colors={
            'DEBUG':    'cyan',
            'INFO':     'green',
            'WARNING':  'yellow',
            'ERROR':    'red',
            'CRITICAL': 'red,bg_white',
        },
        secondary_log_colors={ # Added secondary colors (v1.6)
            'message': {
                'ERROR':    'red',
                'CRITICAL': 'red',
                'WARNING':  'yellow',
                'DEBUG':    'grey'
                # INFO messages will use default color
            }
        },
        style='%'
    )
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(log_level if debug else logging.INFO) # Console level depends on debug flag
    logger.addHandler(console_handler)

    # File Handler (Detailed, Rotating)
    file_format = '%(asctime)s - %(levelname)-8s - [%(name)s:%(funcName)s:%(lineno)d] - %(message)s'
    file_formatter = logging.Formatter(file_format, datefmt='%Y-%m-%d %H:%M:%S')
    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG) # Always log DEBUG level and above to file
        logger.addHandler(file_handler)
    except Exception as e:
        # Fallback if file logging fails (e.g., permissions)
        print(f"Warning: Could not configure file logging to '{LOG_FILE}': {e}", file=sys.stderr)
        if logger: logger.error(f"Could not configure file logging: {e}", exc_info=True)


    # Silence noisy libraries by setting their log level higher
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("google.auth.transport.requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
    logging.getLogger("requests_oauthlib.oauth1_session").setLevel(logging.WARNING)


# --- Lock File Management ---
def acquire_lock():
    """Acquires a lock file to prevent multiple instances."""
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        # Log acquisition only if logging is set up
        if logger: logger.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        if logger: logger.error(f"Another instance might be running. Lock file '{LOCK_FILE}' already exists.")
        print(f"Error: Lock file '{LOCK_FILE}' already exists. Is another instance running?")
        return False
    except OSError as e:
        if logger: logger.error(f"Error acquiring lock file '{LOCK_FILE}': {e}")
        print(f"Error: Could not create lock file '{LOCK_FILE}'. Check permissions.")
        return False

def release_lock():
    """Releases the lock file."""
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            if logger: logger.info(f"Released lock file: {LOCK_FILE}")
        except OSError as e:
            if logger: logger.warning(f"Could not remove lock file '{LOCK_FILE}': {e}")

# --- Cleanup Function (v1.6 Refactor) ---
def cleanup(google_photos_instance):
    """
    Cleans up temporary resources and releases the lock file.
    This function should be called in a 'finally' block to ensure it runs.
    """
    global logger
    # Use logger if available, basic print otherwise
    log_func_info = logger.info if logger else lambda msg: print(f"INFO: {msg}")
    log_func_debug = logger.debug if logger else lambda msg: print(f"DEBUG: {msg}")
    log_func_error = logger.error if logger else lambda msg: print(f"ERROR: {msg}")
    log_func_warning = logger.warning if logger else lambda msg: print(f"WARNING: {msg}")

    log_func_info("Running cleanup...")

    # Explicitly clean up the temp directory using the GooglePhotos method
    if google_photos_instance and hasattr(google_photos_instance, 'cleanup_temp_dir'):
        try:
            log_func_debug("Calling Google Photos temporary directory cleanup...")
            google_photos_instance.cleanup_temp_dir() # Call explicit cleanup
        except Exception as e:
            log_func_error(f"Error during explicit Google Photos temp dir cleanup: {e}", exc_info=True)

    # Attempt to delete the instance (optional, helps trigger __del__ if needed elsewhere, though explicit cleanup is preferred)
    if google_photos_instance:
        try:
            del google_photos_instance
            log_func_debug("Google Photos instance deletion attempted.")
        except Exception as e:
            log_func_warning(f"Error during Google Photos instance deletion: {e}")

    release_lock() # Release lock after other cleanup
    log_func_info("Cleanup complete.")


# --- Main Function ---
def main():
    global logger, shutdown_requested # Declare logger as global

    # Argument Parsing
    parser = argparse.ArgumentParser(description="Transfer Google Photos to SmugMug.")
    parser.add_argument('--google-photos-album-id', help='(Optional) Google Photos Album ID to process.')
    parser.add_argument('--delete-from-google', action='store_true', help='(Simulated) Log deletion from Google Photos after successful transfer.')
    parser.add_argument('--smugmug-album', help='(Optional) Target SmugMug album name (overrides config).')
    parser.add_argument('--smugmug-folder', help='(Optional) Target SmugMug folder path (e.g., "Folder/SubFolder") (overrides config).')
    parser.add_argument('--dry-run', action='store_true', help='Perform a dry run (check existence, log actions, no uploads/deletions).')
    parser.add_argument('--ignore-photos', action='store_true', help='Skip processing photos (images).')
    parser.add_argument('--ignore-videos', action='store_true', help='Skip processing videos.')
    parser.add_argument('--process-heic', action='store_true', help='Attempt to process HEIC files (see README for warnings).')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args()

    # Setup Logging (v1.6 Refactor) - Do this first!
    setup_logging(args.debug)

    # Acquire Lock File
    if not acquire_lock():
        sys.exit(1)

    # Register signal handlers for graceful shutdown (v1.6)
    signal.signal(signal.SIGINT, signal_handler)  # Handle Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler) # Handle termination signals (e.g., kill command)
    # On Windows, SIGBREAK might be relevant for console close, but SIGINT/SIGTERM cover most cases.
    if hasattr(signal, 'SIGBREAK'):
         signal.signal(signal.SIGBREAK, signal_handler)


    # --- Initialization ---
    google_photos = None
    smugmug = None
    start_time = time.time()

    try:
        logger.info(f"--- Starting gp2sm v{__version__} ---")
        logger.info(f"Dry Run Mode: {args.dry_run}")
        logger.info(f"Processing HEIC: {args.process_heic}")
        logger.info(f"Ignoring Photos: {args.ignore_photos}")
        logger.info(f"Ignoring Videos: {args.ignore_videos}")
        logger.info(f"Google Album Filter: {args.google_photos_album_id or 'None (Full Library)'}")
        logger.info(f"Simulated Deletion: {args.delete_from_google}")

        # Initialize SmugMug
        logger.info(f"Initializing SmugMug using config: smugmug_config.json")
        smugmug = SmugMug(config_file='smugmug_config.json')
        try:
            smugmug.load_config()
        except FileNotFoundError:
             smugmug.generate_default_config()
             sys.exit(0)
        except Exception as e:
             logger.critical(f"Failed to load or parse SmugMug config: {e}", exc_info=True)
             print(f"Error: Failed to load SmugMug config. Check '{smugmug.config_file}' and logs.")
             sys.exit(1)

        # Set process_heic based on command line if provided
        if args.process_heic:
            # Ensure config object exists before modifying
            if smugmug.config:
                smugmug.config['process_heic'] = True
            else:
                 # This case shouldn't happen due to exit above, but safety check
                 logger.error("Cannot set process_heic: SmugMug config not loaded.")
                 sys.exit(1)


        # Override album/folder from command line if provided
        if args.smugmug_album:
            logger.info(f"Overriding SmugMug album from command line: '{args.smugmug_album}'")
            if smugmug.config:
                smugmug.config['album_name'] = args.smugmug_album
                smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']
                smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
                smugmug.album_name = args.smugmug_album
                smugmug.album_key = None
                smugmug.album_api_uri = None
            else:
                 logger.error("Cannot override album name: SmugMug config not loaded.")
                 sys.exit(1)


        if args.smugmug_folder:
             logger.info(f"Overriding SmugMug folder from command line: '{args.smugmug_folder}'")
             if smugmug.config:
                 smugmug.config['folder_name'] = args.smugmug_folder
                 smugmug.folder_name = args.smugmug_folder
             else:
                  logger.error("Cannot override folder name: SmugMug config not loaded.")
                  sys.exit(1)


        # Authenticate SmugMug
        if not smugmug.check_config_and_authenticate():
            sys.exit(1)

        # Initialize Google Photos
        logger.info(f"Initializing Google Photos using credentials: google_api_keys.json")
        try:
            google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json')
            if not google_photos.is_authenticated():
                 logger.critical("Google Photos initialization completed, but authentication failed. Check logs.")
                 print("\nError: Google Photos authentication failed. Check logs and credentials.")
                 sys.exit(1)
            logger.info("Google Photos initialized successfully.")
        except GoogleCredentialsNotFoundError as e:
            sys.exit(1)
        except Exception as e:
            logger.critical(f"An critical unexpected error occurred during Google Photos initialization: {e}", exc_info=True)
            print(f"\nError: Unexpected error during Google Photos setup: {e}")
            sys.exit(1)

        # Ensure target SmugMug album exists
        if not smugmug.get_or_create_album_in_path(smugmug.album_name or smugmug.album_key, smugmug.folder_name):
            logger.critical("Failed to find or create the target SmugMug album. Cannot proceed.")
            print("\nError: Could not verify or create the target SmugMug album. Check configuration and permissions.")
            sys.exit(1)
        # Use logger.info instead of success for standard levels
        logger.info(f"Confirmed target SmugMug album. Name: '{smugmug.album_name or '(Using Key)'}', Key: {smugmug.album_key}, URI: {smugmug.album_api_uri}")


        # --- Fetch Google Photos Items ---
        logger.info("Fetching media items from Google Photos...")
        photos = google_photos.get_photos(args.google_photos_album_id)
        total_items = len(photos)
        logger.info(f"Found {total_items} total items in Google Photos.")

        if total_items == 0:
            logger.info("No items found in Google Photos library/album. Nothing to process.")
            print("No items found in Google Photos library/album.")
            sys.exit(0)

        # --- Process Items ---
        processed_count = 0
        uploaded_count = 0
        duplicate_count = 0
        skipped_count = 0
        error_count = 0

        for item_index, original_item in enumerate(photos):
            # Check for shutdown request (v1.6)
            if shutdown_requested:
                 logger.warning("Shutdown requested by signal. Stopping processing loop.")
                 break # Exit the loop gracefully

            # --- Periodic Token Refresh Check ---
            if (item_index > 0 and (item_index + 1) % TOKEN_REFRESH_CHECK_INTERVAL == 0): # Avoid check on first item
                logger.debug(f"Performing periodic Google Photos token check (item {item_index + 1})...")
                if not google_photos.refresh_token_if_needed():
                     logger.warning("Periodic token refresh failed. Attempting re-authentication...")
                     if not google_photos.authenticate():
                          logger.critical("Re-authentication failed after periodic refresh failure. Stopping script.")
                          print("\nError: Google Photos session became invalid and could not be re-authenticated.")
                          # No sys.exit here, let finally block handle cleanup
                          shutdown_requested = True # Trigger graceful exit
                          break # Exit loop
                     else:
                          logger.info("Re-authentication successful after periodic refresh failure.")

            processed_count += 1
            if not isinstance(original_item, dict):
                 logger.error(f"Skipping item {item_index + 1}/{total_items} - Invalid item data format: {original_item}")
                 error_count += 1
                 continue

            media_item_id = original_item.get('id')
            filename = original_item.get('filename')
            mime_type = original_item.get('mimeType')
            product_url = original_item.get('productUrl') # Keep for logging

            if not media_item_id:
                 logger.error(f"Skipping item {item_index + 1}/{total_items} - Missing 'id' field.")
                 error_count += 1
                 continue

            is_video = mime_type and mime_type.startswith('video/')
            item_type = "Video" if is_video else "Photo"
            logger.info("-" * 50)
            logger.info(f"Processing {item_index + 1}/{total_items} - {item_type}: '{filename}' (ID: {media_item_id})")
            print("-" * 30)
            print(f"-> Processing {item_index + 1}/{total_items}: {filename} ({item_type})")


            if not filename or not mime_type:
                logger.warning(f"Skipping item {item_index + 1}/{total_items} (ID: {media_item_id}, URL: {product_url}) due to missing filename or mimeType.")
                skipped_count += 1; error_count += 1
                continue

            _, file_extension = os.path.splitext(filename)

            # Skip based on flags
            if args.ignore_photos and not is_video:
                logger.info(f"Skipping photo '{filename}' due to --ignore-photos flag.")
                skipped_count += 1
                continue
            if args.ignore_videos and is_video:
                logger.info(f"Skipping video '{filename}' due to --ignore-videos flag.")
                skipped_count += 1
                continue

            # HEIC Handling
            is_heic = mime_type == 'image/heic'
            temp_file_path = None
            google_file_hash = None # Use this name consistently now

            if is_heic:
                # Check config AND command line override
                process_heic_config = False
                if smugmug.config and 'process_heic' in smugmug.config:
                    process_heic_config = smugmug.config.get('process_heic', False)
                # CLI flag overrides config
                should_process_heic = args.process_heic or process_heic_config

                if not should_process_heic:
                    logger.info(f"Skipping HEIC file '{filename}' as per configuration.")
                    skipped_count += 1
                    continue
                else:
                    logger.warning(f"Processing HEIC file '{filename}'. NOTE: SmugMug will convert this to JPG. Duplicate check is skipped.")
                    # Download needed for upload later
                    temp_file_path, _, _, _, _, dl_status = google_photos.download_photo(original_item)
                    if not temp_file_path:
                         logger.error(f"Failed to download HEIC file '{filename}' for processing (status {dl_status}). Skipping.")
                         error_count += 1
                         continue

            # --- Image MD5 Check with Re-fetch Logic ---
            # (This block contains the fix from previous steps)
            if not is_video and not is_heic:
                md5_hash = None # Renamed from google_file_hash inside this block for clarity
                if not temp_file_path: # Only download if not already downloaded
                    logger.debug(f"  Downloading image '{filename}' for MD5 check...")
                    temp_file_path, _, _, _, _, error_code = google_photos.download_photo(original_item)

                    if not temp_file_path and error_code in [401, 403]:
                        logger.warning(f"  Initial download failed for MD5 check '{filename}' (Code: {error_code}). Attempting to refresh item details...")
                        try:
                            refetched_item = google_photos.get_media_item(media_item_id)
                            if refetched_item:
                                logger.info(f"  Successfully refreshed item details for '{filename}'. Retrying download once.")
                                temp_file_path, _, _, _, _, error_code = google_photos.download_photo(refetched_item)
                            else:
                                logger.error(f"  Failed to get fresh item details for '{filename}' during MD5 check. Skipping.")
                                error_code = "Re-fetch Failed"
                        except Exception as e:
                            logger.error(f"  Error refreshing/retrying download for '{filename}': {e}. Skipping.", exc_info=True)
                            error_code = f"Re-fetch Error: {e}"

                if not temp_file_path:
                    logger.error(f"  Skipping image '{filename}' due to download error (Final attempt for MD5 check; code: {error_code}).")
                    skipped_count += 1; error_count += 1
                    continue

                md5_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
                if not md5_hash:
                    logger.error(f"  Skipping image '{filename}' due to MD5 calculation failure.")
                    skipped_count += 1; error_count += 1
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                            logger.debug(f"  Removed temp file {temp_file_path} after hash failure.")
                        except Exception as e:
                            logger.warning(f"  Could not remove temp file {temp_file_path} after hash failure: {e}")
                        temp_file_path = None
                    continue

                google_file_hash = md5_hash # Assign to the variable used outside this block
                logger.debug(f"  Calculated MD5 for '{filename}': {google_file_hash}. Proceeding to SmugMug check...")
            # --- End MD5 Check ---

            exists_on_smugmug = False
            log_reason = ""
            target_album_key = smugmug.album_key # Use the confirmed album key

            if not target_album_key:
                 # Should have been set during init/get_or_create_album_in_path
                 logger.error(f"Target SmugMug album key not set. Cannot check existence for '{filename}'. Skipping.")
                 error_count += 1
                 continue

            if is_video:
                log_reason = "filename match"
                exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type)
            elif is_heic:
                log_reason = "HEIC (skipped)"
                exists_on_smugmug = False
                logger.debug("Skipping SmugMug existence check for HEIC file (being processed).")
            else: # Image
                log_reason = "MD5 hash"
                if google_file_hash:
                    exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type, file_hash=google_file_hash)
                else:
                     # This case means MD5 check was required but hash is missing - should have been caught earlier
                     logger.error(f"Logic error: MD5 hash missing for image '{filename}' before SmugMug check. Skipping.")
                     error_count += 1
                     if temp_file_path and os.path.exists(temp_file_path): # Cleanup just in case
                          try: os.remove(temp_file_path)
                          except Exception: pass
                     temp_file_path = None
                     continue


            # --- Handle Duplicates ---
            if exists_on_smugmug:
                logger.info(f"  FOUND on SmugMug ({log_reason}).")
                print(f"   Exists on SmugMug ({log_reason}). Skipping.")
                duplicate_count += 1
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        logger.debug(f"  Removed temp file {temp_file_path} for duplicate item.")
                    except Exception as e:
                        logger.warning(f"  Could not remove temp file {temp_file_path} after duplicate check: {e}")
                    temp_file_path = None

                if args.delete_from_google:
                     google_photos.remove_photo(media_item_id, dry_run=args.dry_run)
                continue

            else:
                 logger.info(f"  Not found on SmugMug ({log_reason}). Will proceed.")

            # --- Prepare for Upload ---
            if args.dry_run:
                logger.info(f"  [DRY RUN] Would attempt upload for '{filename}'.")
                if temp_file_path and os.path.exists(temp_file_path):
                     try: os.remove(temp_file_path); logger.debug("  [DRY RUN] Removed temp file.")
                     except Exception as e_rem: logger.warning(f"  [DRY RUN] Could not remove temp file {temp_file_path}: {e_rem}")
                temp_file_path = None
                uploaded_count += 1
                if args.delete_from_google:
                     google_photos.remove_photo(media_item_id, dry_run=True)
                continue

            # --- Download for Upload (if not already downloaded for MD5/HEIC) ---
            if not temp_file_path or not os.path.exists(temp_file_path):
                logger.debug(f"  Downloading '{filename}' for upload...")
                temp_file_path, _, _, _, _, dl_status = google_photos.download_photo(original_item)

                # Handle download failure with re-fetch (use refetched item for retry)
                if not temp_file_path and dl_status in [401, 403]:
                     logger.warning(f"Download failed for upload (status {dl_status}). Attempting re-fetch for '{filename}' (ID: {media_item_id})")
                     try:
                          refetched_item = google_photos.get_media_item(media_item_id)
                          if refetched_item:
                              logger.info(f"  Successfully refreshed item details for '{filename}'. Retrying download for upload.")
                              temp_file_path, _, _, _, _, dl_status = google_photos.download_photo(refetched_item)
                          else:
                              logger.error(f"Re-fetch attempt failed for '{filename}'. Cannot retry download for upload.")
                              dl_status = "Re-fetch Failed"
                     except Exception as refetch_err:
                         logger.error(f"Error during re-fetch attempt for '{filename}': {refetch_err}", exc_info=True)
                         dl_status = "Re-fetch Error"

                     if not temp_file_path:
                          logger.error(f"Download for upload failed even after re-fetch (status {dl_status}). Skipping item '{filename}'.")
                          error_count += 1
                          continue
                elif not temp_file_path:
                     logger.error(f"Download failed for upload (status {dl_status}). Skipping item '{filename}'.")
                     error_count += 1
                     continue

            # Check again before upload
            if not temp_file_path or not os.path.exists(temp_file_path):
                logger.error(f"Cannot upload '{filename}': Temp file path is missing or file does not exist after download attempt.")
                error_count += 1
                continue

            # --- Perform Upload ---
            logger.info(f"  Uploading '{filename}' to SmugMug album '{smugmug.album_name or smugmug.album_key}'...")
            upload_success = smugmug.upload_media(smugmug.album_api_uri, temp_file_path, filename, mime_type)
            # upload_media handles its own temp file cleanup

            if upload_success:
                # Use logger.info instead of success
                logger.info(f"  Successfully uploaded '{filename}' to SmugMug.")
                uploaded_count += 1
                if args.delete_from_google:
                    google_photos.remove_photo(media_item_id, dry_run=args.dry_run)
            else:
                logger.error(f"  Upload FAILED for '{filename}'.")
                error_count += 1

            # Optional brief pause
            # time.sleep(0.1)

        # --- End of Loop ---
        logger.info("--- Processing Complete ---")

        # --- Final Summary ---
        total_duration = time.time() - start_time
        logger.info("-" * 50)
        logger.info("Final Transfer Summary:")
        logger.info(f"  Items Processed: {processed_count}/{total_items}")
        logger.info(f"  Items Uploaded (or Dry Run success): {uploaded_count}")
        logger.info(f"  Duplicates Found on SmugMug: {duplicate_count}")
        logger.info(f"  Items Skipped (Flags/HEIC/Other): {skipped_count}")
        logger.info(f"  Errors Encountered: {error_count}")
        logger.info(f"  Simulated Deletion Requested: {args.delete_from_google} (Simulated - API Unsupported)")
        logger.info(f"  Total execution time: {total_duration:.2f} seconds")
        logger.info("-" * 50)
        # Use logger.info for final status message
        if error_count == 0 and (processed_count - skipped_count - error_count) == (uploaded_count + duplicate_count): # More precise check
             logger.info("Script finished successfully.")
        elif error_count > 0:
             logger.warning(f"Script finished with {error_count} errors. Please review logs.")
        else:
             logger.warning("Script finished, but some items may have been skipped unexpectedly or counts may not align.")

        print("\n" + "=" * 60)
        print("Transfer Summary:")
        print(f"- Items Processed: {processed_count}/{total_items}")
        print(f"- Uploaded: {uploaded_count}")
        print(f"- Duplicates Found: {duplicate_count}")
        print(f"- Skipped (Flags/HEIC): {skipped_count}")
        print(f"- Errors: {error_count}")
        print(f"- Total Time: {total_duration:.2f} seconds")
        print(f"- Log file: {LOG_FILE}")
        print("=" * 60)

    except KeyboardInterrupt:
        # This block might not be reached if signal handler sets shutdown_requested
        if not shutdown_requested: # Log only if not already handled by signal
             logger.warning("Keyboard interrupt detected outside signal handler. Shutting down gracefully...")
             print("\nKeyboard interrupt received. Cleaning up...")
             shutdown_requested = True # Ensure flag is set for finally block
    except Exception as e:
        logger.critical(f"A critical unexpected error occurred in main: {e}", exc_info=True)
        print(f"\nA critical unexpected error occurred: {e}. Check the log file '{LOG_FILE}' for details.")
    finally:
        # Crucial: Cleanup called by finally block ensures it runs even on errors/interrupts
        cleanup(google_photos)

if __name__ == "__main__":
    main()