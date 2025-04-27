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
# Make sure google_photos_module has the fail-fast download_photo & cleanup_temp_dir
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
# DONE v1.6: Change HEIC detection to use filename extension
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

    # Prevent adding handlers multiple times
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
    log_func_info = logger.info if logger else lambda msg: print(f"INFO: {msg}")
    log_func_debug = logger.debug if logger else lambda msg: print(f"DEBUG: {msg}")
    log_func_error = logger.error if logger else lambda msg: print(f"ERROR: {msg}")
    log_func_warning = logger.warning if logger else lambda msg: print(f"WARNING: {msg}")

    log_func_info("Running cleanup...")

    if google_photos_instance and hasattr(google_photos_instance, 'cleanup_temp_dir'):
        try:
            log_func_debug("Calling Google Photos temporary directory cleanup...")
            google_photos_instance.cleanup_temp_dir()
        except Exception as e:
            log_func_error(f"Error during explicit Google Photos temp dir cleanup: {e}", exc_info=True)

    if google_photos_instance:
        try:
            del google_photos_instance
            log_func_debug("Google Photos instance deletion attempted.")
        except Exception as e:
            log_func_warning(f"Error during Google Photos instance deletion: {e}")

    release_lock()
    log_func_info("Cleanup complete.")


# --- Main Function ---
def main():
    global logger, shutdown_requested

    parser = argparse.ArgumentParser(description="Transfer Google Photos to SmugMug.")
    parser.add_argument('--google-photos-album-id', help='(Optional) Google Photos Album ID to process.')
    parser.add_argument('--delete-from-google', action='store_true', help='(Simulated) Log deletion from Google Photos.')
    parser.add_argument('--smugmug-album', help='(Optional) Target SmugMug album name (overrides config).')
    parser.add_argument('--smugmug-folder', help='(Optional) Target SmugMug folder path (overrides config).')
    parser.add_argument('--dry-run', action='store_true', help='Perform a dry run.')
    parser.add_argument('--ignore-photos', action='store_true', help='Skip processing photos.')
    parser.add_argument('--ignore-videos', action='store_true', help='Skip processing videos.')
    parser.add_argument('--process-heic', action='store_true', help='Attempt to process HEIC files.')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args()

    # Setup Logging (v1.6)
    setup_logging(args.debug)

    # Acquire Lock File
    if not acquire_lock(): sys.exit(1)

    # Register signal handlers (v1.6)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, 'SIGBREAK'): signal.signal(signal.SIGBREAK, signal_handler)

    # --- Initialization ---
    google_photos = None; smugmug = None; start_time = time.time()
    try:
        logger.info(f"--- Starting gp2sm v{__version__} ---")
        logger.info(f"Dry Run Mode: {args.dry_run}"); logger.info(f"Processing HEIC: {args.process_heic}")
        logger.info(f"Ignoring Photos: {args.ignore_photos}"); logger.info(f"Ignoring Videos: {args.ignore_videos}")
        logger.info(f"Google Album Filter: {args.google_photos_album_id or 'None'}"); logger.info(f"Simulated Deletion: {args.delete_from_google}")

        logger.info(f"Initializing SmugMug using config: smugmug_config.json")
        smugmug = SmugMug(config_file='smugmug_config.json')
        try: smugmug.load_config()
        except FileNotFoundError: smugmug.generate_default_config(); sys.exit(0)
        except Exception as e: logger.critical(f"Failed SmugMug load: {e}", exc_info=True); print(f"Error: Failed SmugMug load."); sys.exit(1)

        if args.process_heic:
            if smugmug.config: smugmug.config['process_heic'] = True
            else: logger.error("SmugMug config not loaded."); sys.exit(1)
        if args.smugmug_album:
            logger.info(f"Overriding SmugMug album: '{args.smugmug_album}'")
            if smugmug.config:
                smugmug.config['album_name'] = args.smugmug_album; smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']; smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
                smugmug.album_name = args.smugmug_album; smugmug.album_key = None; smugmug.album_api_uri = None
            else: logger.error("SmugMug config not loaded."); sys.exit(1)
        if args.smugmug_folder:
             logger.info(f"Overriding SmugMug folder: '{args.smugmug_folder}'")
             if smugmug.config: smugmug.config['folder_name'] = args.smugmug_folder; smugmug.folder_name = args.smugmug_folder
             else: logger.error("SmugMug config not loaded."); sys.exit(1)

        if not smugmug.check_config_and_authenticate(): sys.exit(1)

        logger.info(f"Initializing Google Photos using credentials: google_api_keys.json")
        try:
            google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json')
            if not google_photos.is_authenticated(): logger.critical("Google Photos auth failed."); print("\nError: GP auth failed."); sys.exit(1)
            logger.info("Google Photos initialized successfully.")
        except GoogleCredentialsNotFoundError as e: sys.exit(1)
        except Exception as e: logger.critical(f"GP init error: {e}", exc_info=True); print(f"\nError: GP setup error: {e}"); sys.exit(1)

        if not smugmug.get_or_create_album_in_path(smugmug.album_name or smugmug.album_key, smugmug.folder_name):
            logger.critical("Failed target SmugMug album setup."); print("\nError: Could not setup SmugMug album."); sys.exit(1)
        logger.info(f"Confirmed target SmugMug album. Name: '{smugmug.album_name or '(Key)'}', Key: {smugmug.album_key}, URI: {smugmug.album_api_uri}")

        logger.info("Fetching media items from Google Photos...")
        photos = google_photos.get_photos(args.google_photos_album_id)
        total_items = len(photos)
        logger.info(f"Found {total_items} total items in Google Photos.")
        if total_items == 0: logger.info("No items found."); print("No items found."); sys.exit(0)

        processed_count = 0; uploaded_count = 0; duplicate_count = 0; skipped_count = 0; error_count = 0
        assume_stale_urls = False # <-- Stateful heuristic flag

        for item_index, original_item in enumerate(photos):
            if shutdown_requested: logger.warning("Shutdown requested. Stopping."); break # v1.6 check

            # Periodic Token Refresh
            if (item_index > 0 and (item_index + 1) % TOKEN_REFRESH_CHECK_INTERVAL == 0):
                logger.debug(f"Periodic GP token check (item {item_index + 1})...")
                if not google_photos.refresh_token_if_needed():
                     logger.warning("Periodic refresh failed. Re-authenticating...")
                     if not google_photos.authenticate():
                          logger.critical("Re-auth failed. Stopping."); print("\nError: GP session invalid."); shutdown_requested = True; break
                     else: logger.info("Re-auth successful.")

            processed_count += 1
            if not isinstance(original_item, dict): logger.error(f"Skip item {item_index+1} - Invalid data"); error_count += 1; continue
            media_item_id = original_item.get('id'); filename = original_item.get('filename'); mime_type = original_item.get('mimeType')
            if not media_item_id: logger.error(f"Skip item {item_index+1} - Missing ID"); error_count += 1; continue

            is_video = mime_type and mime_type.startswith('video/'); item_type = "Video" if is_video else "Photo"
            logger.info("-" * 50); logger.info(f"Processing {item_index + 1}/{total_items} - {item_type}: '{filename}' (ID: {media_item_id})")
            print("-" * 30); print(f"-> Processing {item_index + 1}/{total_items}: {filename} ({item_type})")

            if not filename or not mime_type: logger.warning(f"Skip item {item_index + 1} (ID: {media_item_id}) missing filename/mimeType."); skipped_count += 1; error_count += 1; continue
            _, file_extension = os.path.splitext(filename)

            if args.ignore_photos and not is_video: logger.info(f"Skip photo '{filename}' (--ignore-photos)."); skipped_count += 1; continue
            if args.ignore_videos and is_video: logger.info(f"Skip video '{filename}' (--ignore-videos)."); skipped_count += 1; continue

            # --- HEIC Handling (Using filename extension) ---
            is_heic = filename and filename.lower().endswith('.heic') # v1.6 change
            temp_file_path = None; google_file_hash = None; should_process_heic = False
            if is_heic:
                process_heic_config = False
                if smugmug.config: process_heic_config = smugmug.config.get('process_heic', False)
                should_process_heic = args.process_heic or process_heic_config
                if not should_process_heic: logger.info(f"Skipping HEIC file '{filename}'."); skipped_count += 1; continue
                else: logger.warning(f"Processing HEIC file '{filename}'. NOTE: Converted to JPG by SmugMug. Duplicate check skipped.")

            # ==============================================================
            # --- Download & MD5 Logic Block (v1.6 - stateful heuristic) ---
            # ==============================================================
            download_required_for_md5 = (not is_video and not is_heic) # Only for standard images
            current_item_data = original_item
            error_code = None; download_attempted = False

            # --- Stateful Heuristic Check ---
            if download_required_for_md5 and assume_stale_urls and media_item_id:
                 logger.info(f"  Proactively refreshing item details for '{filename}' (MD5 check).")
                 try:
                      proactively_refreshed_item = google_photos.get_media_item(media_item_id)
                      if proactively_refreshed_item:
                           logger.debug(f"  Proactive refresh successful for MD5 check '{filename}'.")
                           current_item_data = proactively_refreshed_item
                           assume_stale_urls = False
                      else: logger.warning(f"  Proactive refresh failed for MD5 check '{filename}'."); assume_stale_urls = False
                 except Exception as proactive_refetch_err: logger.warning(f"  Error during proactive refresh (MD5): {proactive_refetch_err}"); assume_stale_urls = False

            # --- Download Attempt (for MD5) ---
            if download_required_for_md5:
                 logger.debug(f"  Attempting download for MD5 check '{filename}' {'using fresh data.' if current_item_data != original_item else 'using original data.'}")
                 download_attempted = True
                 temp_file_path, _, _, _, _, error_code = google_photos.download_photo(current_item_data)

                 # --- Reactive Re-fetch on 401/403 ---
                 if not temp_file_path and error_code in [401, 403]:
                      logger.warning(f"  Download attempt failed for MD5 check (Code: {error_code}). Refreshing...")
                      original_error_code = error_code
                      assume_stale_urls = True # Mark future URLs as likely stale
                      try:
                           refetched_item_reactive = google_photos.get_media_item(media_item_id)
                           if refetched_item_reactive:
                                logger.info(f"  Successfully refreshed item details. Retrying download once (MD5).")
                                current_item_data = refetched_item_reactive # Update data to use
                                temp_file_path, _, _, _, _, retry_error_code = google_photos.download_photo(current_item_data)
                                error_code = retry_error_code # Update error code with retry result
                                if temp_file_path: logger.info(f"  Download succeeded on reactive retry (MD5)."); assume_stale_urls = False # Success resets flag
                                else: logger.error(f"  Download FAILED on reactive retry (MD5; Code: {error_code}).") # Flag stays True
                           else: logger.error(f"  Failed reactive re-fetch (MD5). Skipping."); error_code = "Re-fetch Failed"
                      except Exception as e: logger.error(f"  Error reactive re-fetch/retry (MD5): {e}", exc_info=True); error_code = f"Re-fetch Error: {e}"
                      # Ensure error_code has a value if retry failed
                      if not temp_file_path and error_code is None: error_code = original_error_code or "Unknown Retry Failure"
                 elif temp_file_path:
                      assume_stale_urls = False # Reset flag if initial download worked

            # --- Calculate MD5 if applicable and successful ---
            if not is_video and not is_heic: # Standard images
                if temp_file_path and os.path.exists(temp_file_path):
                    logger.debug(f"Calculating MD5 hash for {temp_file_path}")
                    google_file_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
                    if not google_file_hash:
                        logger.error(f"  Skipping image '{filename}' - MD5 calc failed.")
                        skipped_count += 1; error_count += 1
                        if os.path.exists(temp_file_path):
                            try: os.remove(temp_file_path); logger.debug(f"  Removed temp file after hash failure.")
                            except Exception as e: logger.warning(f"  Could not remove temp file after hash failure: {e}")
                        temp_file_path = None
                        continue # Skip item
                    logger.debug(f"  Calculated MD5: {google_file_hash}.")
                elif download_attempted: # Download was tried but failed
                    logger.error(f"  Skipping image '{filename}' as download failed (Code: {error_code}).")
                    skipped_count += 1; error_count += 1
                    continue # Skip item

            # ==============================================================
            # --- End Download & MD5 Logic Block ---
            # ==============================================================

            # --- Check SmugMug Existence ---
            exists_on_smugmug = False; log_reason = ""; target_album_key = smugmug.album_key
            if not target_album_key: logger.error(f"SmugMug key missing for '{filename}'. Skip check."); error_count += 1; continue

            if is_video: log_reason = "filename"; exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type)
            elif is_heic and should_process_heic: log_reason = "HEIC (skipped)"; exists_on_smugmug = False; logger.debug("Skipping SmugMug check (processing HEIC).")
            elif not is_video and not is_heic: # Standard image
                log_reason = "MD5 hash"
                if google_file_hash: exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type, file_hash=google_file_hash)
                else: logger.warning(f"MD5 missing for image '{filename}', cannot check SmugMug."); skipped_count += 1; error_count += 1; continue # Skip if hash missing

            # --- Handle Duplicates ---
            if exists_on_smugmug:
                logger.info(f"  FOUND on SmugMug ({log_reason})."); print(f"   Exists ({log_reason}). Skipping.")
                duplicate_count += 1
                if temp_file_path and os.path.exists(temp_file_path): # Cleanup if downloaded
                    try: os.remove(temp_file_path); logger.debug(f"  Removed temp file for duplicate.")
                    except Exception as e: logger.warning(f"  Could not remove temp file after duplicate check: {e}")
                    temp_file_path = None
                if args.delete_from_google: google_photos.remove_photo(media_item_id, dry_run=args.dry_run)
                continue
            else: logger.info(f"  Not found on SmugMug ({log_reason}). Proceeding.")

            # --- Prepare for Upload ---
            if args.dry_run:
                logger.info(f"  [DRY RUN] Would upload '{filename}'.")
                if temp_file_path and os.path.exists(temp_file_path):
                     try: os.remove(temp_file_path); logger.debug("  [DRY RUN] Removed temp file.")
                     except Exception as e_rem: logger.warning(f"  [DRY RUN] Could not remove temp file: {e_rem}")
                temp_file_path = None
                uploaded_count += 1
                if args.delete_from_google: google_photos.remove_photo(media_item_id, dry_run=True)
                continue

            # --- Download for Upload (if not already available) ---
            if not temp_file_path or not os.path.exists(temp_file_path):
                 logger.debug(f"Downloading '{filename}' specifically for upload...")
                 # Re-apply stateful heuristic logic here too
                 current_item_data_upload = original_item
                 error_code_upload = None
                 download_attempted_upload = False

                 if assume_stale_urls and media_item_id:
                      logger.info(f"  Proactively refreshing '{filename}' (upload phase).")
                      try:
                           proactively_refreshed_item_upload = google_photos.get_media_item(media_item_id)
                           if proactively_refreshed_item_upload:
                                logger.debug(f"  Proactive refresh successful (upload phase).")
                                current_item_data_upload = proactively_refreshed_item_upload
                                assume_stale_urls = False
                           else: logger.warning(f"  Proactive refresh failed (upload phase)."); assume_stale_urls = False
                      except Exception as proactive_refetch_err_upload: logger.warning(f"  Error proactive refresh (upload): {proactive_refetch_err_upload}"); assume_stale_urls = False

                 logger.debug(f"  Attempting download for upload '{filename}' {'using fresh data.' if current_item_data_upload != original_item else 'using original data.'}")
                 download_attempted_upload = True
                 temp_file_path, _, _, _, _, error_code_upload = google_photos.download_photo(current_item_data_upload)

                 # Reactive Re-fetch on 401/403 for upload download
                 if not temp_file_path and error_code_upload in [401, 403]:
                      logger.warning(f"  Download attempt failed for upload (Code: {error_code_upload}). Refreshing...")
                      original_error_code_upload = error_code_upload
                      assume_stale_urls = True
                      try:
                           refetched_item_reactive_upload = google_photos.get_media_item(media_item_id)
                           if refetched_item_reactive_upload:
                                logger.info(f"  Refreshed item details. Retrying download once (upload).")
                                current_item_data_upload = refetched_item_reactive_upload
                                temp_file_path, _, _, _, _, retry_error_code_upload = google_photos.download_photo(current_item_data_upload)
                                error_code_upload = retry_error_code_upload
                                if temp_file_path: logger.info(f"  Download succeeded on reactive retry (upload)."); assume_stale_urls = False
                                else: logger.error(f"  Download FAILED on reactive retry (upload; Code: {error_code_upload}).")
                           else: logger.error(f"  Failed reactive re-fetch (upload). Skipping."); error_code_upload = "Re-fetch Failed"
                      except Exception as e: logger.error(f"  Error reactive re-fetch/retry (upload): {e}", exc_info=True); error_code_upload = f"Re-fetch Error: {e}"
                      if not temp_file_path and error_code_upload is None: error_code_upload = original_error_code_upload or "Unknown Retry Failure"
                 elif temp_file_path:
                     assume_stale_urls = False

                 # Final check before upload
                 if not temp_file_path or not os.path.exists(temp_file_path):
                      logger.error(f"Cannot upload '{filename}': Download failed before upload step (Code: {error_code_upload}).")
                      error_count += 1
                      continue

            # --- Perform Upload ---
            logger.info(f"  Uploading '{filename}' to SmugMug album '{smugmug.album_name or smugmug.album_key}'...")
            upload_success = smugmug.upload_media(smugmug.album_api_uri, temp_file_path, filename, mime_type)

            if upload_success:
                logger.info(f"  Successfully uploaded '{filename}' to SmugMug.")
                uploaded_count += 1
                if args.delete_from_google: google_photos.remove_photo(media_item_id, dry_run=args.dry_run)
            else: logger.error(f"  Upload FAILED for '{filename}'."); error_count += 1

        # --- End of Loop ---
        logger.info("--- Processing Complete ---")

        # --- Final Summary ---
        total_duration = time.time() - start_time
        logger.info("-" * 50); logger.info("Final Transfer Summary:")
        logger.info(f"  Items Processed: {processed_count}/{total_items}"); logger.info(f"  Items Uploaded (or Dry Run success): {uploaded_count}")
        logger.info(f"  Duplicates Found on SmugMug: {duplicate_count}"); logger.info(f"  Items Skipped (Flags/HEIC/Other): {skipped_count}")
        logger.info(f"  Errors Encountered: {error_count}"); logger.info(f"  Simulated Deletion Requested: {args.delete_from_google} (Simulated)")
        logger.info(f"  Total execution time: {total_duration:.2f} seconds"); logger.info("-" * 50)
        items_attempted = total_items - skipped_count; successful_outcomes = uploaded_count + duplicate_count
        if error_count == 0 and items_attempted == successful_outcomes: logger.info("Script finished successfully.")
        elif error_count > 0: logger.warning(f"Script finished with {error_count} errors. Please review logs.")
        else: logger.warning("Script finished, but item counts suggest discrepancies.")

        print("\n" + "=" * 60); print("Transfer Summary:")
        print(f"- Items Processed: {processed_count}/{total_items}"); print(f"- Uploaded: {uploaded_count}")
        print(f"- Duplicates Found: {duplicate_count}"); print(f"- Skipped (Flags/HEIC): {skipped_count}")
        print(f"- Errors: {error_count}"); print(f"- Total Time: {total_duration:.2f} seconds"); print(f"- Log file: {LOG_FILE}"); print("=" * 60)

    except KeyboardInterrupt:
        if not shutdown_requested: logger.warning("Keyboard interrupt detected."); print("\nKeyboard interrupt. Cleaning up."); shutdown_requested = True
    except Exception as e: logger.critical(f"A critical unexpected error occurred in main: {e}", exc_info=True); print(f"\nCritical error: {e}. Check log '{LOG_FILE}'.")
    finally: cleanup(google_photos)

if __name__ == "__main__":
    main()
