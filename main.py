# Google Photos to SmugMug Transfer Script (v1.9)
# Updates DB with refreshed Google Photos item details (baseUrl).
# Refactored conditional styling in SmugMug check block.
# Removed pre-download check/refresh for missing baseUrl from main.
# Added explicit logging for MD5 hash calculation.
# Clarified SmugMug check log messages.
# Truncated Google ID in logs and added "Starting..." messages.

__version__ = "1.9" # Version remains 1.9 as requested

# Standard library imports
import argparse
import logging
import os
import sys
import time
import json # For parsing metadata from DB
from logging.handlers import RotatingFileHandler
import signal # For signal handling

# Third-party imports
import colorlog

# Local module imports
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug, DEFAULT_SMUGMUG_CONFIG
from database_manager import ( # Import new DB manager and constants
    DatabaseManager, DB_FILE_DEFAULT, TABLE_NAME,
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
# Truncate Google ID length in logs for readability
LOG_ID_TRUNCATE_LEN = 8

# --- Global Variables ---
logger = None
shutdown_requested = False
google_photos_instance_global = None
db_manager_global = None

# --- Signal Handling (No changes) ---
def signal_handler(sig, frame):
    global shutdown_requested, logger
    if not shutdown_requested:
        signal_name = f"Signal {sig}"
        try: signal_name = signal.Signals(sig).name
        except ValueError: pass
        log_func = getattr(logger, 'warning', print)
        print_func = print
        log_func(f"Received signal {signal_name}. Initiating graceful shutdown...")
        print_func(f"\n>>> Signal {signal_name} received. Stopping after current item and cleaning up... <<<")
        shutdown_requested = True
    else:
        if logger: logger.debug(f"Shutdown already in progress. Received signal {sig} again.")
        print(">>> Shutdown already requested. Please wait. <<<")


# --- Logging Setup (No changes) ---
def setup_logging(debug=False):
    global logger
    log_level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()
    logger.setLevel(log_level)

    console_format = ('%(asctime)s - %(log_color)s%(levelname)-8s%(reset)s - '
                      '[%(name)s:%(funcName)s:%(lineno)d] - '
                      '%(message_log_color)s%(message)s%(reset)s')
    console_formatter = colorlog.ColoredFormatter(
        console_format, datefmt='%Y-%m-%d %H:%M:%S', reset=True,
        log_colors={'DEBUG':'cyan','INFO':'green','WARNING':'yellow','ERROR':'red','CRITICAL':'red,bg_white'},
        secondary_log_colors={'message': {'ERROR':'red','CRITICAL':'red','WARNING':'yellow'}}, style='%'
    )
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addHandler(console_handler)

    file_format = '%(asctime)s - %(levelname)-8s - [%(name)s:%(funcName)s:%(lineno)d] - %(message)s'
    file_formatter = logging.Formatter(file_format, datefmt='%Y-%m-%d %H:%M:%S')
    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"Warning: Could not configure file logging to '{LOG_FILE}': {e}", file=sys.stderr)
        if logger: logger.error(f"Failed to set up file logging handler: {e}", exc_info=True)

    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("google.auth.transport.requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
    logging.getLogger("requests_oauthlib.oauth1_session").setLevel(logging.WARNING)
    logging.getLogger("database_manager").setLevel(log_level)


# --- Lock File Management (No changes) ---
def acquire_lock():
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
        if logger: logger.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        log_msg = f"Lock file '{LOCK_FILE}' already exists. Another instance may be running."
        if logger: logger.error(log_msg)
        print(f"Error: {log_msg}", file=sys.stderr)
        print("If sure no other instance is running, delete the lock file.", file=sys.stderr)
        return False
    except OSError as e:
        log_msg = f"Error acquiring lock file '{LOCK_FILE}': {e}"
        if logger: logger.error(log_msg, exc_info=True)
        print(f"Error: {log_msg}", file=sys.stderr)
        return False

def release_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            if logger: logger.info(f"Released lock file: {LOCK_FILE}")
        except OSError as e:
            if logger: logger.warning(f"Could not remove lock file '{LOCK_FILE}': {e}", exc_info=True)
            print(f"Warning: Could not remove lock file '{LOCK_FILE}': {e}", file=sys.stderr)


# --- Cleanup Function (No changes) ---
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


# --- Main Function ---
def main():
    global logger, shutdown_requested, google_photos_instance_global, db_manager_global
    parser = argparse.ArgumentParser(
        description="Transfer Google Photos to SmugMug using SQLite for state tracking.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # Argument parsing
    parser.add_argument('--google-photos-album-id', help='(Optional) Process only media from this Google Photos Album ID. Used only when populating the database initially.')
    parser.add_argument('--ignore-photos', action='store_true', help='Mark photos to be skipped during processing.')
    parser.add_argument('--ignore-videos', action='store_true', help='Mark videos to be skipped during processing.')
    parser.add_argument('--process-heic', action='store_true', help='Process HEIC files (upload as converted JPGs, no duplicate check). Default is false (skip HEIC).')
    parser.add_argument('--smugmug-album', help='(Optional) Target SmugMug album name. Overrides config file setting. Will be created if it doesn\'t exist.')
    parser.add_argument('--smugmug-folder', help='(Optional) Target SmugMug folder path (e.g., "Vacations/Europe"). Overrides config file setting.')
    parser.add_argument('--dry-run', action='store_true', help='Simulate transfer: check existence, log actions, but do not upload or modify DB status beyond checks.')
    parser.add_argument('--delete-from-google', action='store_true', help='(Simulated) Log deletion from Google Photos after successful processing (status UPLOADED_SUCCESS or DUPLICATE_*).')
    parser.add_argument('--debug', action='store_true', help='Enable detailed debug logging.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--db-file', default=DB_FILE_DEFAULT, help='Path to the SQLite database file for storing transfer state.')
    parser.add_argument('--force-refresh-list', action='store_true', help='Ignore existing DB content and re-fetch the complete list from Google Photos.')
    parser.add_argument('--retry-errors', action='store_true', help='Attempt to re-process items currently marked with an error status in the database.')
    parser.add_argument('--reset-errors', action='store_true', help='Reset all items with an error status back to PENDING before starting.')

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

    start_time = time.time()

    try:
        logger.info(f"--- Starting gp2sm v{__version__} ---")
        logger.info(f"Using database file: {args.db_file}")
        logger.info(f"Command line arguments: {vars(args)}")

        # --- Initialize Database Manager ---
        try:
             db_manager = DatabaseManager(db_file=args.db_file)
             db_manager_global = db_manager
             logger.info("Database manager initialized.")
        except Exception as e:
             logger.critical(f"Failed to initialize database manager: {e}", exc_info=True)
             print(f"\nError: Could not initialize database {args.db_file}.", file=sys.stderr)
             sys.exit(1)

        # --- Initialize SmugMug ---
        logger.info("Initializing SmugMug module...")
        try:
            smugmug = SmugMug(config_file='smugmug_config.json')
            smugmug.load_config()
        except FileNotFoundError:
             logger.warning(f"SmugMug config file '{smugmug.config_file}' not found.")
             if smugmug.generate_default_config():
                 logger.info("Default SmugMug config generated. Please edit it and run again.")
                 print("\nPlease edit 'smugmug_config.json' and run again.")
                 sys.exit(0)
             else:
                 logger.critical("Failed to generate default SmugMug config file.")
                 print("\nError: Failed to generate default SmugMug config file.", file=sys.stderr)
                 sys.exit(1)
        except Exception as e:
             logger.critical(f"Failed to load SmugMug configuration: {e}", exc_info=True)
             print(f"\nError: Failed to load SmugMug configuration from '{smugmug.config_file}'.", file=sys.stderr)
             sys.exit(1)

        # Apply CLI overrides
        if args.process_heic:
            if smugmug.config: smugmug.config['process_heic'] = True; logger.info("Override: HEIC processing enabled.")
            else: logger.error("Cannot apply HEIC override: SmugMug config not loaded."); sys.exit(1)
        if args.smugmug_album:
             logger.info(f"Override: Target SmugMug album name: '{args.smugmug_album}'")
             if smugmug.config:
                 smugmug.config['album_name'] = args.smugmug_album
                 smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key']
                 smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
             else: logger.error("Cannot apply album override: SmugMug config not loaded."); sys.exit(1)
        if args.smugmug_folder:
             logger.info(f"Override: Target SmugMug folder path: '{args.smugmug_folder}'")
             if smugmug.config: smugmug.config['folder_name'] = args.smugmug_folder
             else: logger.error("Cannot apply folder override: SmugMug config not loaded."); sys.exit(1)

        # Authenticate SmugMug
        logger.info("Authenticating with SmugMug...")
        if not smugmug.check_config_and_authenticate():
            logger.critical("SmugMug authentication or configuration check failed.")
            sys.exit(1)
        logger.info("SmugMug initialization and authentication successful.")

        # --- Ensure Target SmugMug Album Exists ---
        target_album_name = smugmug.config.get('album_name') if smugmug.config else None
        target_folder_path = smugmug.config.get('folder_name') if smugmug.config else None
        target_album_key = smugmug.config.get('album_key') if smugmug.config else None

        if target_album_name and target_album_name != DEFAULT_SMUGMUG_CONFIG['album_name']:
             logger.info(f"Ensuring SmugMug album '{target_album_name}' exists in folder '{target_folder_path or 'root'}'...")
             if not smugmug.get_or_create_album_in_path(target_album_name, target_folder_path):
                  logger.critical("Failed to find or create target SmugMug album by name.")
                  print("\nError: Could not set up the target SmugMug album by name.", file=sys.stderr)
                  sys.exit(1)
             target_album_key = smugmug.album_key
        elif target_album_key and target_album_key != DEFAULT_SMUGMUG_CONFIG['album_key']:
             logger.info(f"Using existing SmugMug album specified by key: {target_album_key}")
             smugmug.album_key = target_album_key
             smugmug.album_api_uri = smugmug.config.get('album_api_uri')
             smugmug.album_name = None
        else:
             logger.critical("No valid target SmugMug album specified in config or via CLI.")
             print("\nError: Specify target SmugMug album in config or use --smugmug-album.", file=sys.stderr)
             sys.exit(1)

        if not target_album_key:
             logger.critical("Failed to determine target SmugMug album key.")
             sys.exit(1)
        logger.info(f"Confirmed SmugMug Target Key: {target_album_key}, URI: {smugmug.album_api_uri}, Folder: '{target_folder_path or 'root'}'")


        # --- Initialize Google Photos ---
        logger.info("Initializing Google Photos module...")
        try:
            google_photos = GooglePhotos(credentials_file='google_api_keys.json', token_file='google_photos_token.json')
            google_photos_instance_global = google_photos
            if not google_photos.is_authenticated():
                 logger.critical("Google Photos module initialized but not authenticated.")
                 print("\nError: Google Photos authentication failed.", file=sys.stderr)
                 sys.exit(1)
            logger.info("Google Photos initialization and authentication successful.")
        except GoogleCredentialsNotFoundError as e:
             print(f"\nError: {e}", file=sys.stderr)
             print("Ensure Google API credentials file is correctly named and placed.", file=sys.stderr)
             sys.exit(1)
        except Exception as e:
             logger.critical(f"Failed to initialize Google Photos module: {e}", exc_info=True)
             print(f"\nError: Failed to initialize Google Photos: {e}.", file=sys.stderr)
             sys.exit(1)

        # --- Populate Database if Needed ---
        total_db_items = db_manager.get_item_count()
        if total_db_items == 0 or args.force_refresh_list:
            if args.force_refresh_list: logger.warning("Force refresh: Re-fetching list from Google Photos.")
            else: logger.info("Database empty. Fetching list from Google Photos.")
            print("Fetching initial media list from Google Photos...")
            photos_list = google_photos.get_photos(args.google_photos_album_id)
            logger.info(f"Fetched {len(photos_list)} items from Google Photos.")
            if photos_list:
                 added_count = db_manager.add_item_batch(photos_list, target_album_key)
                 logger.info(f"Populated database with {added_count} new items.")
                 total_db_items = db_manager.get_item_count()
            else:
                 logger.info("No items found in Google Photos source.")
                 print("No items found in Google Photos source.")
                 sys.exit(0)
        else:
            logger.info(f"Database contains {total_db_items} items. Resuming.")
            print(f"Found {total_db_items} items tracked in the database.")

        # --- Reset Errors if Requested ---
        if args.reset_errors:
             db_manager.reset_failed_items()
             total_db_items = db_manager.get_item_count()


        # --- Get Items to Process from Database ---
        logger.info("Fetching items to process from database...")
        items_to_process = db_manager.get_items_to_process(retry_errors=args.retry_errors)
        total_items_to_process = len(items_to_process)
        logger.info(f"Found {total_items_to_process} items requiring processing.")
        if total_items_to_process == 0:
             logger.info("No items require processing.")
             print("No items require processing.")
             final_stats = db_manager.get_stats()
             logger.info(f"Final Database Stats: {final_stats}")
             print("\nFinal Stats:")
             for status, count in sorted(final_stats.items()):
                  if count > 0: print(f"- {status}: {count}")
             sys.exit(0)


        # --- Process Items from DB ---
        processed_in_run = 0; uploaded_in_run = 0; duplicates_in_run = 0; skipped_in_run = 0; errors_in_run = 0

        for item_index, item_row in enumerate(items_to_process):
            current_item_number_in_batch = item_index + 1
            if shutdown_requested:
                logger.warning(f"Shutdown requested. Stopping processing loop.")
                break

            # Extract data from DB row
            google_id = item_row['google_id']
            filename = item_row['filename']
            mime_type = item_row['mime_type']
            current_status = item_row['status']
            db_md5_hash = item_row['md5_hash']
            # Create a mutable dictionary for the current item's details
            item_details_for_download = dict(item_row)

            is_video = mime_type.startswith('video/')
            item_type = "Video" if is_video else "Photo"

            # *** TRUNCATED ID FOR LOGGING ***
            truncated_id = f"{google_id[:LOG_ID_TRUNCATE_LEN]}...{google_id[-LOG_ID_TRUNCATE_LEN:]}" if len(google_id) > LOG_ID_TRUNCATE_LEN * 2 else google_id
            log_identifier = f"Item {current_item_number_in_batch}/{total_items_to_process} (ID: {truncated_id}, File: '{filename}', Type: {item_type}, Status: {current_status})"
            # *** END TRUNCATED ID ***

            logger.info("-" * 50)
            logger.info(f"Processing {log_identifier}")
            print("-" * 30)
            print(f"-> Processing {current_item_number_in_batch}/{total_items_to_process}: {filename} ({item_type}) [Status: {current_status}]")

            processed_in_run += 1
            temp_file_path = None
            current_md5_hash = db_md5_hash
            refreshed_details = None # Reset for each item

            # --- Apply Filters ---
            if args.ignore_photos and not is_video:
                 logger.info(f"{log_identifier}: Marked to skip (Photo).")
                 db_manager.update_item_status(google_id, STATUS_SKIPPED_FILTER, error_message="Skipped via --ignore-photos")
                 skipped_in_run += 1
                 continue
            if args.ignore_videos and is_video:
                 logger.info(f"{log_identifier}: Marked to skip (Video).")
                 db_manager.update_item_status(google_id, STATUS_SKIPPED_FILTER, error_message="Skipped via --ignore-videos")
                 skipped_in_run += 1
                 continue

            # --- HEIC Handling ---
            is_heic = filename.lower().endswith('.heic')
            should_process_heic = args.process_heic or (smugmug.config and smugmug.config.get('process_heic', False))
            if is_heic:
                if not should_process_heic:
                    logger.info(f"{log_identifier}: Marked to skip (HEIC).")
                    db_manager.update_item_status(google_id, STATUS_SKIPPED_HEIC, error_message="HEIC processing not enabled")
                    skipped_in_run += 1
                    continue
                else:
                    logger.warning(f"{log_identifier}: Processing HEIC (no duplicate check).")


            # --- Download & MD5 Check (if needed) ---
            needs_download_for_hash = not is_video and not is_heic and not current_md5_hash
            if needs_download_for_hash:
                logger.debug(f"{log_identifier}: MD5 hash needed. Calling download function...")
                # Pass the dictionary from the DB row
                temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details_for_download)

                if not temp_file_path:
                    logger.error(f"{log_identifier}: Download failed for MD5 check.")
                    db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, error_message="Download failed during hash check")
                    errors_in_run += 1
                    skipped_in_run += 1
                    continue # Skip to next item

                # Update DB if details were refreshed *during* download
                if refreshed_details:
                     new_base_url = refreshed_details.get('baseUrl')
                     new_metadata_json = json.dumps(refreshed_details.get('mediaMetadata', {}))
                     db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                     refreshed_details = None # Reset

                # *** ADDED HASH LOGGING HERE ***
                logger.info(f"{log_identifier}: Calculating MD5 hash...") # User feedback
                print("   Calculating MD5 hash...") # Console feedback
                calculated_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
                # *** END ADDED HASH LOGGING ***

                if not calculated_hash:
                    logger.error(f"{log_identifier}: MD5 hash calculation failed.")
                    db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, error_message="MD5 calculation failed")
                    errors_in_run += 1
                    skipped_in_run += 1
                    if os.path.exists(temp_file_path):
                         try: os.remove(temp_file_path)
                         except Exception as e: logger.warning(f"Could not remove temp file {temp_file_path}: {e}")
                    continue

                logger.debug(f"{log_identifier}: Calculated MD5: {calculated_hash}. Updating DB.")
                current_md5_hash = calculated_hash
                db_manager.update_item_status(google_id, STATUS_HASHED, md5_hash=current_md5_hash)
            elif not is_video and not is_heic:
                 logger.debug(f"{log_identifier}: MD5 hash already in DB: {current_md5_hash}")


            # --- Check SmugMug Existence ---
            if current_status in [STATUS_DUPLICATE_FILENAME, STATUS_DUPLICATE_HASH, STATUS_UPLOADED_SUCCESS]:
                 logger.debug(f"{log_identifier}: Skipping SM check (terminal status '{current_status}').")
                 processed_in_run -= 1
                 continue
            elif is_heic and should_process_heic:
                 logger.debug(f"{log_identifier}: Skipping SM check (HEIC processing enabled).")
                 exists_on_smugmug = False
                 log_reason = "HEIC (skipped check)"
                 if current_status not in [STATUS_SMUGMUG_CHECKED_NOT_FOUND, STATUS_DOWNLOADED_FOR_UPLOAD, STATUS_UPLOAD_ATTEMPTED]:
                      db_manager.update_item_status(google_id, STATUS_SMUGMUG_CHECKED_NOT_FOUND, error_message="HEIC check skipped")
            else:
                 if current_status in [STATUS_PENDING, STATUS_HASHED]:
                      exists_on_smugmug = False
                      log_reason = ""
                      target_album_key_for_check = smugmug.album_key
                      # *** ADDED SMUGMUG CHECK LOGGING ***
                      logger.info(f"{log_identifier}: Checking SmugMug for duplicates...")
                      print("   Checking SmugMug for duplicates...")
                      # *** END ADDED SMUGMUG CHECK LOGGING ***
                      logger.debug(f"{log_identifier}: Checking existence on SmugMug album key: {target_album_key_for_check}")

                      if is_video:
                           log_reason = "filename match"
                           exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type)
                      elif not is_video and not is_heic: # Standard image check
                           log_reason = "MD5 hash match"
                           if current_md5_hash:
                                exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type, file_hash=current_md5_hash)
                           else:
                                logger.error(f"{log_identifier}: Cannot check SmugMug, MD5 hash missing.")
                                db_manager.update_item_status(google_id, STATUS_ERROR_HASHING, error_message="MD5 missing for SM check")
                                errors_in_run += 1
                                skipped_in_run += 1
                                if temp_file_path and os.path.exists(temp_file_path):
                                     try: os.remove(temp_file_path)
                                     except Exception as e: logger.warning(f"Could not remove temp file {temp_file_path}: {e}")
                                continue

                      if exists_on_smugmug:
                           duplicate_status = STATUS_DUPLICATE_FILENAME if is_video else STATUS_DUPLICATE_HASH
                           logger.info(f"{log_identifier}: Found on SmugMug (checked via {log_reason}). Marking duplicate.")
                           print(f"   Exists on SmugMug ({log_reason}). Skipping.")
                           db_manager.update_item_status(google_id, duplicate_status, error_message=f"Duplicate check via {log_reason}")
                           duplicates_in_run += 1
                           if temp_file_path and os.path.exists(temp_file_path):
                                try: os.remove(temp_file_path)
                                except Exception as e: logger.warning(f"Could not remove temp file {temp_file_path}: {e}")
                           if args.delete_from_google:
                                google_photos.remove_photo(google_id, dry_run=args.dry_run)
                           continue
                      else:
                           logger.info(f"{log_identifier}: Checked SmugMug via {log_reason}: Not found.")
                           db_manager.update_item_status(google_id, STATUS_SMUGMUG_CHECKED_NOT_FOUND, error_message=f"SM check via {log_reason} - not found")
                 else:
                      logger.debug(f"{log_identifier}: Skipping SM check (status is '{current_status}').")


            # --- Prepare for Upload ---
            if args.dry_run:
                logger.info(f"{log_identifier}: [DRY RUN] Would upload.")
                print("   [DRY RUN] Skipping upload.")
                uploaded_in_run += 1
                if temp_file_path and os.path.exists(temp_file_path):
                     try:
                          os.remove(temp_file_path)
                          logger.debug("  [DRY RUN] Removed temp file.")
                     except Exception as e:
                          logger.warning(f"  [DRY RUN] Could not remove temp file {temp_file_path}: {e}")
                if args.delete_from_google:
                     google_photos.remove_photo(google_id, dry_run=True)
                continue


            # --- Download for Upload (if needed) ---
            if not temp_file_path or not os.path.exists(temp_file_path):
                 logger.debug(f"{log_identifier}: File not available locally. Calling download...")
                 # Pass the potentially updated item_details dictionary
                 temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details_for_download)

                 if not temp_file_path:
                      logger.error(f"{log_identifier}: Download failed before upload.")
                      db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, error_message="Download failed before upload")
                      errors_in_run += 1
                      skipped_in_run += 1
                      continue

                 # Update DB if details were refreshed *during* this download
                 if refreshed_details:
                      new_base_url = refreshed_details.get('baseUrl')
                      new_metadata_json = json.dumps(refreshed_details.get('mediaMetadata', {}))
                      db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                      refreshed_details = None # Reset
            else:
                 logger.debug(f"{log_identifier}: Using existing local file for upload: {temp_file_path}")


            # --- Perform Upload ---
            target_album_uri_for_upload = smugmug.album_api_uri
            logger.info(f"{log_identifier}: Uploading to SmugMug URI: {target_album_uri_for_upload}...")
            print(f"   Uploading to SmugMug...") # Console feedback
            db_manager.update_item_status(google_id, STATUS_UPLOAD_ATTEMPTED, increment_attempt=True)

            upload_success = smugmug.upload_media(target_album_uri_for_upload, temp_file_path, filename, mime_type)

            if upload_success:
                logger.info(f"{log_identifier}: Upload successful.")
                print("   Upload successful.")
                db_manager.update_item_status(google_id, STATUS_UPLOADED_SUCCESS)
                uploaded_in_run += 1
                if args.delete_from_google:
                     google_photos.remove_photo(google_id, dry_run=args.dry_run)
            else:
                logger.error(f"{log_identifier}: Upload failed.")
                print("   Upload FAILED.")
                last_sm_error = "Upload failed (check SmugMug logs)"
                db_manager.update_item_status(google_id, STATUS_ERROR_UPLOAD_FAILED, error_message=last_sm_error)
                errors_in_run += 1


        # --- End of Loop ---
        logger.info("-" * 50)
        if not shutdown_requested: logger.info(f"--- Finished processing batch ---")
        else: logger.warning("--- Processing loop terminated by shutdown request ---")


        # --- Final Summary ---
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
        final_stats = db_manager.get_stats()
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
        if shutdown_requested: print("- Status: Terminated by user")
        elif errors_in_run > 0: print(f"- Status: Completed run with {errors_in_run} errors")
        else: print("- Status: Completed run successfully")
        print(f"- Run Time: {total_duration:.2f} sec")
        print("-" * 60)
        print("Overall Database Stats:")
        for status, count in sorted(final_stats.items()):
             if count > 0: print(f"- {status}: {count}")
        print(f"- Detailed Log: {LOG_FILE}")
        print(f"- Database File: {args.db_file}")
        print("=" * 60)

    except KeyboardInterrupt:
        if not shutdown_requested:
             logger.warning("Keyboard interrupt detected directly in main.")
             print("\nKeyboard Interrupt. Cleaning up...")
             shutdown_requested = True
    except Exception as e:
        logger.critical(f"Critical unexpected error in main execution: {e}", exc_info=True)
        print(f"\nCritical Error: {e}. Check log '{LOG_FILE}'.", file=sys.stderr)
        shutdown_requested = True
    finally:
        # --- Cleanup ---
        cleanup(google_photos_instance_global, db_manager_global)
        exit_code = 1 if errors_in_run > 0 or shutdown_requested else 0
        logger.info(f"Exiting script with code {exit_code}.")
        sys.exit(exit_code)

if __name__ == "__main__":
    # __version__ remains "1.9" as requested
    main()
