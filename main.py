# Google Photos to SmugMug Transfer Script
#
# This script facilitates transferring media from Google Photos to SmugMug.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#
__version__ = "1.6"  # Includes stateful download, signal handling, logging improvements

# Standard library imports
import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
import signal # Added for signal handling

# Third-party imports
import colorlog

# Local module imports
# Make sure google_photos_module has the fail-fast download_photo
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
# Make sure smugmug_module has attribute validation fix & removed sys import
from smugmug_module import SmugMug, DEFAULT_SMUGMUG_CONFIG

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
# DONE v1.6: Implement smarter download retry (stateful heuristic)
# DONE v1.6: Implement graceful shutdown via signal handling
# DONE v1.6: Refactor logging setup
# DONE v1.6: Enhance console colors
# DONE v1.6: Refactor cleanup logic
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
        # Initialize signal_name before the try block with a default
        signal_name = f"Signal {sig}"
        try:
             # Attempt to get the more descriptive signal name
             signal_name = signal.Signals(sig).name
        except ValueError:
             # If the signal number isn't recognized, signal_name keeps the default value
             pass # No action needed here, default is already set

        # Use logger if available, otherwise print
        log_func = logger.warning if logger else lambda msg: print(f"WARNING: {msg}")
        print_func = print

        # Now log using the guaranteed-to-be-defined signal_name
        log_func(f"Received signal {signal_name}. Initiating graceful shutdown...")
        print_func(f"\nSignal {signal_name} received. Cleaning up...")
        shutdown_requested = True
    else:
        # In the 'else' block, just log the signal number for simplicity
        if logger:
            logger.debug(f"Shutdown already requested. Received signal {sig} again.")

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
        print(f"Warning: Could not configure file logging to '{LOG_FILE}': {e}", file=sys.stderr)
        if logger: logger.error(f"Could not configure file logging: {e}", exc_info=True)


    # Silence noisy libraries
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

# --- Cleanup Function (v1.6 Enhanced) ---
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

    # Attempt to delete the instance (optional)
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
    global logger, shutdown_requested # Declare globals

    # Argument Parsing
    parser = argparse.ArgumentParser(description="Transfer Google Photos to SmugMug.")
    # (Keep existing args)
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

    # Setup Logging (v1.6)
    setup_logging(args.debug)

    # Acquire Lock File
    if not acquire_lock():
        sys.exit(1)

    # Register signal handlers (v1.6)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
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

        # Set process_heic (check config exists)
        if args.process_heic:
            if smugmug.config: smugmug.config['process_heic'] = True
            else: logger.error("Cannot set process_heic: SmugMug config not loaded."); sys.exit(1)

        # Override album/folder (check config exists)
        if args.smugmug_album:
            logger.info(f"Overriding SmugMug album from command line: '{args.smugmug_album}'")
            if smugmug.config:
                smugmug.config['album_name'] = args.smugmug_album
                smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']
                smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
                smugmug.album_name = args.smugmug_album; smugmug.album_key = None; smugmug.album_api_uri = None
            else: logger.error("Cannot override album name: SmugMug config not loaded."); sys.exit(1)

        if args.smugmug_folder:
             logger.info(f"Overriding SmugMug folder from command line: '{args.smugmug_folder}'")
             if smugmug.config:
                 smugmug.config['folder_name'] = args.smugmug_folder
                 smugmug.folder_name = args.smugmug_folder
             else: logger.error("Cannot override folder name: SmugMug config not loaded."); sys.exit(1)

        # Authenticate SmugMug
        if not smugmug.check_config_and_authenticate(): sys.exit(1)

        # Initialize Google Photos
        logger.info(f"Initializing Google Photos using credentials: google_api_keys.json")
        try:
            google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json')
            if not google_photos.is_authenticated():
                 logger.critical("Google Photos initialization completed, but authentication failed.")
                 print("\nError: Google Photos authentication failed. Check logs and credentials.")
                 sys.exit(1)
            logger.info("Google Photos initialized successfully.")
        except GoogleCredentialsNotFoundError as e: sys.exit(1)
        except Exception as e:
            logger.critical(f"An critical unexpected error occurred during Google Photos initialization: {e}", exc_info=True)
            print(f"\nError: Unexpected error during Google Photos setup: {e}")
            sys.exit(1)

        # Ensure target SmugMug album exists
        if not smugmug.get_or_create_album_in_path(smugmug.album_name or smugmug.album_key, smugmug.folder_name):
            logger.critical("Failed to find or create the target SmugMug album. Cannot proceed.")
            print("\nError: Could not verify or create the target SmugMug album.")
            sys.exit(1)
        logger.info(f"Confirmed target SmugMug album. Name: '{smugmug.album_name or '(Using Key)'}', Key: {smugmug.album_key}, URI: {smugmug.album_api_uri}")


        # --- Fetch Google Photos Items ---
        logger.info("Fetching media items from Google Photos...")
        photos = google_photos.get_photos(args.google_photos_album_id)
        total_items = len(photos)
        logger.info(f"Found {total_items} total items in Google Photos.")

        if total_items == 0:
            logger.info("No items found in Google Photos library/album. Nothing to process."); print("No items found.")
            sys.exit(0)

        # --- Process Items ---
        processed_count = 0; uploaded_count = 0; duplicate_count = 0; skipped_count = 0; error_count = 0
        assume_stale_urls = False # Flag for stateful heuristic

        for item_index, original_item in enumerate(photos):
            # Check for shutdown request (v1.6)
            if shutdown_requested:
                 logger.warning("Shutdown requested by signal. Stopping processing loop.")
                 break

            # Periodic Token Refresh Check
            if (item_index > 0 and (item_index + 1) % TOKEN_REFRESH_CHECK_INTERVAL == 0):
                logger.debug(f"Performing periodic Google Photos token check (item {item_index + 1})...")
                if not google_photos.refresh_token_if_needed():
                     logger.warning("Periodic token refresh failed. Attempting re-authentication...")
                     if not google_photos.authenticate():
                          logger.critical("Re-authentication failed. Stopping script.")
                          print("\nError: Google Photos session invalid & could not re-authenticate.")
                          shutdown_requested = True; break # Trigger cleanup and exit loop
                     else:
                          logger.info("Re-authentication successful.")

            processed_count += 1
            if not isinstance(original_item, dict): logger.error(f"Skipping item {item_index+1}/{total_items} - Invalid data"); error_count += 1; continue

            media_item_id = original_item.get('id'); filename = original_item.get('filename'); mime_type = original_item.get('mimeType')
            if not media_item_id: logger.error(f"Skipping item {item_index+1}/{total_items} - Missing ID"); error_count += 1; continue

            is_video = mime_type and mime_type.startswith('video/')
            item_type = "Video" if is_video else "Photo"
            logger.info("-" * 50)
            logger.info(f"Processing {item_index + 1}/{total_items} - {item_type}: '{filename}' (ID: {media_item_id})")
            print("-" * 30); print(f"-> Processing {item_index + 1}/{total_items}: {filename} ({item_type})")

            if not filename or not mime_type: logger.warning(f"Skipping item {item_index + 1}/{total_items} (ID: {media_item_id}) due to missing filename/mimeType."); skipped_count += 1; error_count += 1; continue
            _, file_extension = os.path.splitext(filename)

            if args.ignore_photos and not is_video: logger.info(f"Skipping photo '{filename}' (--ignore-photos)."); skipped_count += 1; continue
            if args.ignore_videos and is_video: logger.info(f"Skipping video '{filename}' (--ignore-videos)."); skipped_count += 1; continue

            is_heic = filename and filename.lower().endswith('.heic')
            temp_file_path = None; google_file_hash = None
            should_process_heic = False # Default
            if is_heic:
                process_heic_config = False
                if smugmug.config: process_heic_config = smugmug.config.get('process_heic', False)
                should_process_heic = args.process_heic or process_heic_config
                if not should_process_heic: logger.info(f"Skipping HEIC file '{filename}'."); skipped_count += 1; continue
                else: logger.warning(f"Processing HEIC file '{filename}'. NOTE: Converted to JPG by SmugMug. Duplicate check skipped.")

            # --- Download & MD5 Logic Block (v1.6 - stateful heuristic) ---
            download_required = (not is_video and not is_heic) or (is_heic and should_process_heic) or (not args.dry_run and not (is_video or (is_heic and should_process_heic))) # Need download if image for MD5, or if HEIC/Video for upload
            current_item_data = original_item
            error_code = None # Initialize error code

            # Check if we need to proactively refresh
            if download_required and assume_stale_urls and media_item_id:
                 logger.info(f"  Proactively refreshing item details for '{filename}' due to previous 401/403.")
                 try:
                      proactively_refreshed_item = google_photos.get_media_item(media_item_id)
                      if proactively_refreshed_item:
                           logger.debug(f"  Proactive refresh successful for '{filename}'. Using fresh data.")
                           current_item_data = proactively_refreshed_item
                           assume_stale_urls = False # Reset flag
                      else:
                           logger.warning(f"  Proactive refresh failed for '{filename}'. Will attempt with original data.")
                           assume_stale_urls = False # Reset flag anyway? Yes, maybe it was transient.
                 except Exception as proactive_refetch_err:
                      logger.warning(f"  Error during proactive refresh for '{filename}': {proactive_refetch_err}. Will attempt with original data.")
                      assume_stale_urls = False # Reset flag

            # Attempt download if required
            if download_required:
                 logger.debug(f"  Attempting download for '{filename}' {'using proactively refreshed data.' if current_item_data != original_item else 'using original data.'}")
                 temp_file_path, _, _, _, _, error_code = google_photos.download_photo(current_item_data)

                 # Reactive Re-fetch Logic (if first attempt failed with 401/403)
                 if not temp_file_path and error_code in [401, 403]:
                      logger.warning(f"  Download attempt failed for '{filename}' (Code: {error_code}). Attempting reactive refresh...")
                      assume_stale_urls = True # Set flag: URLs from original batch are likely bad
                      try:
                           refetched_item_reactive = google_photos.get_media_item(media_item_id)
                           if refetched_item_reactive:
                                logger.info(f"  Successfully refreshed item details for '{filename}'. Retrying download once.")
                                current_item_data = refetched_item_reactive # Update with latest data
                                temp_file_path, _, _, _, _, error_code = google_photos.download_photo(current_item_data)
                                if temp_file_path: logger.info(f"  Download succeeded on reactive retry for '{filename}'."); assume_stale_urls = False # Reset flag on success
                                else: logger.error(f"  Download FAILED on reactive retry for '{filename}' (Code: {error_code}).") # Flag remains True
                           else:
                                logger.error(f"  Failed to get fresh item details for '{filename}' during reactive refresh. Skipping.")
                                error_code = "Re-fetch Failed"
                      except Exception as e:
                           logger.error(f"  Error during reactive refresh/retry for '{filename}': {e}. Skipping.", exc_info=True)
                           error_code = f"Re-fetch Error: {e}"
                 elif temp_file_path:
                      assume_stale_urls = False # Reset flag if download succeeded without 401/403

                 # If still no file after all attempts and download was required for subsequent steps
                 if not temp_file_path and ((not is_video and not is_heic) or not args.dry_run):
                      logger.error(f"  Skipping item '{filename}' due to download failure (Final code: {error_code}).")
                      skipped_count += 1; error_count += 1
                      continue # Skip this item completely

            # Calculate MD5 if needed (standard image) and download succeeded
            if not is_video and not is_heic:
                if temp_file_path and os.path.exists(temp_file_path):
                    logger.debug(f"Calculating MD5 hash for downloaded file: {temp_file_path}")
                    google_file_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
                    if not google_file_hash:
                        logger.error(f"  Skipping image '{filename}' due to MD5 calculation failure.")
                        skipped_count += 1; error_count += 1
                        if os.path.exists(temp_file_path):
                            try: os.remove(temp_file_path); logger.debug(f"  Removed temp file {temp_file_path} after hash failure.")
                            except Exception as e: logger.warning(f"  Could not remove temp file {temp_file_path} after hash failure: {e}")
                        temp_file_path = None
                        continue
                    logger.debug(f"  Calculated MD5 for '{filename}': {google_file_hash}.")
                else:
                    # If download failed but was needed for MD5, we skipped earlier
                    pass # Should have already continued


            # --- Check existence on SmugMug ---
            exists_on_smugmug = False; log_reason = ""
            target_album_key = smugmug.album_key
            if not target_album_key: logger.error(f"Target SmugMug key missing for '{filename}'. Skip existence check."); error_count += 1; continue

            if is_video:
                log_reason = "filename match"; exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type)
            elif is_heic and should_process_heic:
                log_reason = "HEIC (skipped)"; exists_on_smugmug = False; logger.debug("Skipping SmugMug check (processing HEIC).")
            elif not is_video and not is_heic: # Standard image
                log_reason = "MD5 hash"
                if google_file_hash: exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type, file_hash=google_file_hash)
                else: logger.warning(f"MD5 missing for image '{filename}', cannot check SmugMug existence."); skipped_count += 1; error_count += 1; continue # Skip if hash missing

            # Handle Duplicates
            if exists_on_smugmug:
                logger.info(f"  FOUND on SmugMug ({log_reason})."); print(f"   Exists on SmugMug ({log_reason}). Skipping.")
                duplicate_count += 1
                if temp_file_path and os.path.exists(temp_file_path): # Cleanup if downloaded
                    try: os.remove(temp_file_path); logger.debug(f"  Removed temp file {temp_file_path} for duplicate.")
                    except Exception as e: logger.warning(f"  Could not remove temp file {temp_file_path} after duplicate check: {e}")
                    temp_file_path = None
                if args.delete_from_google: google_photos.remove_photo(media_item_id, dry_run=args.dry_run)
                continue
            else:
                 logger.info(f"  Not found on SmugMug ({log_reason}). Will proceed.")


            # --- Prepare for Upload ---
            if args.dry_run:
                logger.info(f"  [DRY RUN] Would attempt upload for '{filename}'.")
                if temp_file_path and os.path.exists(temp_file_path): # Cleanup if downloaded
                     try: os.remove(temp_file_path); logger.debug("  [DRY RUN] Removed temp file.")
                     except Exception as e_rem: logger.warning(f"  [DRY RUN] Could not remove temp file {temp_file_path}: {e_rem}")
                temp_file_path = None
                uploaded_count += 1
                if args.delete_from_google: google_photos.remove_photo(media_item_id, dry_run=True)
                continue

            # --- Download for Upload (if not already downloaded) ---
            # The logic block above should have already downloaded if needed.
            # If temp_file_path is still None here, it means download failed or wasn't required for MD5/HEIC.
            if not temp_file_path or not os.path.exists(temp_file_path):
                 # This indicates an item that needs upload but failed download earlier.
                 # The earlier 'continue' should have caught this. Log error if reached.
                 logger.error(f"Logic error or prior download failure: Temp file for '{filename}' needed for upload but missing. Skipping.")
                 error_count += 1
                 continue

            # --- Perform Upload ---
            logger.info(f"  Uploading '{filename}' to SmugMug album '{smugmug.album_name or smugmug.album_key}'...")
            upload_success = smugmug.upload_media(smugmug.album_api_uri, temp_file_path, filename, mime_type)

            if upload_success:
                logger.info(f"  Successfully uploaded '{filename}' to SmugMug.")
                uploaded_count += 1
                if args.delete_from_google: google_photos.remove_photo(media_item_id, dry_run=args.dry_run)
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
        # Final status logging
        items_attempted = total_items - skipped_count
        successful_outcomes = uploaded_count + duplicate_count
        if error_count == 0 and items_attempted == successful_outcomes:
             logger.info("Script finished successfully.")
        elif error_count > 0:
             logger.warning(f"Script finished with {error_count} errors. Please review logs.")
        else:
             logger.warning("Script finished, but item counts suggest some discrepancies. Please review logs.")

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
        if not shutdown_requested: # Log only if not already handled by signal
             logger.warning("Keyboard interrupt detected outside signal handler. Shutting down gracefully...")
             print("\nKeyboard interrupt received. Cleaning up...")
             shutdown_requested = True # Ensure flag is set
    except Exception as e:
        logger.critical(f"A critical unexpected error occurred in main: {e}", exc_info=True)
        print(f"\nA critical unexpected error occurred: {e}. Check log file '{LOG_FILE}'.")
    finally:
        cleanup(google_photos)

if __name__ == "__main__":
    main()
