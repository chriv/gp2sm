# Google Photos to SmugMug Transfer Script (v1.9)
# Added configuration snapshot check on resume.
# Fixed ImportError for table name constant.
# Renames database file on successful completion.

__version__ = "1.9" # Version remains 1.9 as requested

# Standard library imports
import argparse
import logging
import os
import sys
import time
import json # For parsing metadata from DB
import datetime # For timestamp in filename
from logging.handlers import RotatingFileHandler
import signal # For signal handling

# Third-party imports
import colorlog

# Local module imports
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug, DEFAULT_SMUGMUG_CONFIG
from database_manager import ( # Import new DB manager and constants
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
    parser.add_argument('--google-photos-album-id', help='(Optional) Process only media from this Google Photos Album ID. Used only when initially populating the database.')
    parser.add_argument('--ignore-photos', action='store_true', help='Mark photos to be skipped during processing.')
    parser.add_argument('--ignore-videos', action='store_true', help='Mark videos to be skipped during processing.')
    parser.add_argument('--process-heic', action='store_true', help='Process HEIC files (upload as converted JPGs, no duplicate check). Default is false (skip HEIC).')
    parser.add_argument('--smugmug-album', help='(Optional) Target SmugMug album name. Overrides the `album_name`, `album_key`, and `album_api_uri` settings in the config file. Will be created if it doesn\'t exist.')
    parser.add_argument('--smugmug-folder', help='(Optional) Target SmugMug folder path (e.g., "Vacations/Europe"). Overrides the `folder_name` setting in the config file.')
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
    # Flag to track if cleanup/rename was handled in the main try block
    cleanup_handled_in_try = False

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

        # Apply CLI overrides to potentially loaded config
        current_smugmug_album_arg = args.smugmug_album
        current_smugmug_folder_arg = args.smugmug_folder
        current_google_album_arg = args.google_photos_album_id # Capture CLI Google Album ID

        if args.process_heic:
            if smugmug.config: smugmug.config['process_heic'] = True; logger.info("Override: HEIC processing enabled.")
            else: logger.error("Cannot apply HEIC override: SmugMug config not loaded."); sys.exit(1)
        if current_smugmug_album_arg:
             logger.info(f"Override: Target SmugMug album name from CLI: '{current_smugmug_album_arg}'")
             if smugmug.config:
                 smugmug.config['album_name'] = current_smugmug_album_arg
                 smugmug.config['album_key'] = DEFAULT_SMUGMUG_CONFIG['album_key'] # Reset key/uri if name provided
                 smugmug.config['album_api_uri'] = DEFAULT_SMUGMUG_CONFIG['album_api_uri']
             else: logger.error("Cannot apply album override: SmugMug config not loaded."); sys.exit(1)
        if current_smugmug_folder_arg:
             logger.info(f"Override: Target SmugMug folder path from CLI: '{current_smugmug_folder_arg}'")
             if smugmug.config:
                 smugmug.config['folder_name'] = current_smugmug_folder_arg
             else: logger.error("Cannot apply folder override: SmugMug config not loaded."); sys.exit(1)

        # Authenticate SmugMug (must happen before ensuring album exists)
        logger.info("Authenticating with SmugMug...")
        if not smugmug.check_config_and_authenticate():
            logger.critical("SmugMug authentication or configuration check failed.")
            sys.exit(1)
        logger.info("SmugMug initialization and authentication successful.")

        # --- Determine Target SmugMug Album/Folder from CURRENT config/args ---
        current_target_album_name = smugmug.config.get('album_name') if smugmug.config else None
        current_target_folder_path = smugmug.config.get('folder_name') if smugmug.config else None
        current_target_album_key = smugmug.config.get('album_key') if smugmug.config else None
        current_target_album_uri = smugmug.config.get('album_api_uri') if smugmug.config else None

        # --- Ensure Target SmugMug Album Exists (based on current config/args) ---
        if current_target_album_name and current_target_album_name != DEFAULT_SMUGMUG_CONFIG['album_name']:
             logger.info(f"Ensuring SmugMug album '{current_target_album_name}' exists in folder '{current_target_folder_path or 'root'}' (based on current settings)...")
             if not smugmug.get_or_create_album_in_path(current_target_album_name, current_target_folder_path):
                  logger.critical("Failed to find or create target SmugMug album by name.")
                  print("\nError: Could not set up the target SmugMug album by name.", file=sys.stderr)
                  sys.exit(1)
             current_target_album_key = smugmug.album_key
             current_target_album_uri = smugmug.album_api_uri
        elif current_target_album_key and current_target_album_key != DEFAULT_SMUGMUG_CONFIG['album_key']:
             logger.info(f"Using existing SmugMug album specified by key: {current_target_album_key} (based on current settings)")
             smugmug.album_key = current_target_album_key
             smugmug.album_api_uri = current_target_album_uri
             smugmug.album_name = None
        else:
             logger.critical("No valid target SmugMug album specified in current config or via CLI.")
             print("\nError: Specify target SmugMug album in config or use --smugmug-album.", file=sys.stderr)
             sys.exit(1)

        logger.info(f"Current Settings Target - SmugMug Key: {current_target_album_key}, URI: {current_target_album_uri}, Folder: '{current_target_folder_path or 'root'}'")

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

        # --- Populate Database or Check Config Snapshot ---
        is_initial_run = False
        total_db_items = db_manager.get_item_count()
        stored_config = db_manager.get_stored_config()

        if total_db_items == 0 or args.force_refresh_list:
            is_initial_run = True
            if args.force_refresh_list: logger.warning("Force refresh: Re-fetching list from Google Photos and saving config snapshot.")
            else: logger.info("Database empty. Fetching list from Google Photos and saving config snapshot.")

            print("Fetching initial media list from Google Photos...")
            photos_list = google_photos.get_photos(current_google_album_arg)
            logger.info(f"Fetched {len(photos_list)} items from Google Photos.")

            if photos_list:
                 if not db_manager.save_initial_config(current_target_album_key, current_target_album_uri, current_target_folder_path, current_google_album_arg):
                      logger.error("Failed to save initial configuration snapshot to database. Proceeding without snapshot.")

                 added_count = db_manager.add_item_batch(photos_list, current_target_album_key)
                 logger.info(f"Populated database with {added_count} new items.")
                 total_db_items = db_manager.get_item_count()
            else:
                 logger.info("No items found in Google Photos source. Database remains empty.")
                 print("No items found in Google Photos source.")
                 sys.exit(0)
        else:
            # Resuming from existing DB - Check config consistency
            logger.info(f"Database contains {total_db_items} items. Checking config consistency...")
            print(f"Found {total_db_items} items tracked in the database. Checking configuration...")

            if stored_config:
                stored_sm_key = stored_config.get('smugmug_album_key')
                stored_sm_folder = stored_config.get('smugmug_folder_name')
                stored_gp_album = stored_config.get('google_album_id')

                current_folder_norm = current_target_folder_path if current_target_folder_path else None
                stored_folder_norm = stored_sm_folder if stored_sm_folder else None
                current_gp_album_norm = current_google_album_arg if current_google_album_arg else None
                stored_gp_album_norm = stored_gp_album if stored_gp_album else None

                mismatch = False
                if current_target_album_key != stored_sm_key:
                    logger.warning(f"SmugMug Album Key mismatch! Current='{current_target_album_key}', Stored='{stored_sm_key}'")
                    mismatch = True
                if current_folder_norm != stored_folder_norm:
                    logger.warning(f"SmugMug Folder mismatch! Current='{current_folder_norm or 'Root'}', Stored='{stored_folder_norm or 'Root'}'")
                    mismatch = True
                if current_gp_album_norm != stored_gp_album_norm:
                    logger.warning(f"Google Photos Album ID mismatch! Current='{current_gp_album_norm or 'Library'}', Stored='{stored_gp_album_norm or 'Library'}'")
                    mismatch = True

                if mismatch:
                    logger.warning("="*60)
                    logger.warning("CONFIGURATION MISMATCH DETECTED!")
                    logger.warning("Current settings differ from stored settings for this transfer.")
                    logger.warning("CONTINUING WITH STORED settings from the database:")
                    logger.warning(f"  - SmugMug Album Key: {stored_sm_key}")
                    logger.warning(f"  - SmugMug Folder:    {stored_folder_norm or 'Root'}")
                    logger.warning(f"  - Google Album ID:   {stored_gp_album_norm or 'Entire Library'}")
                    logger.warning("To use NEW settings, stop (Ctrl+C) and either:")
                    logger.warning(f"  1. Delete database: '{args.db_file}'")
                    logger.warning(f"  2. Run with '--force-refresh-list'")
                    logger.warning("="*60)
                    print("\n" + "="*60)
                    print("WARNING: CONFIGURATION MISMATCH DETECTED!")
                    print(">>> CONTINUING WITH STORED SETTINGS FROM DATABASE <<<")
                    print(f"    Target SmugMug Album Key: {stored_sm_key}")
                    print(f"    Target SmugMug Folder:    {stored_folder_norm or 'Root'}")
                    print(f"    Source Google Album ID:   {stored_gp_album_norm or 'Entire Library'}")
                    print("\nTo use NEW settings, stop now (Ctrl+C) and either:")
                    print(f"  1. Delete database file: {args.db_file}")
                    print("  2. Run again with --force-refresh-list")
                    print("="*60 + "\n")
                    time.sleep(5)

                    smugmug.album_key = stored_sm_key
                    smugmug.album_api_uri = stored_config.get('smugmug_album_uri')
                    smugmug.folder_name = stored_folder_norm
                    smugmug.album_name = None
                    current_target_album_key = stored_sm_key
                    current_target_album_uri = smugmug.album_api_uri
                    current_target_folder_path = stored_folder_norm
                    current_google_album_arg = stored_gp_album_norm

                else:
                    logger.info("Configuration matches stored snapshot. Proceeding with resume.")
                    print("Configuration matches stored state. Resuming transfer...")
            else:
                logger.warning("Existing database found, but no stored configuration snapshot.")
                logger.warning("Proceeding with CURRENT configuration settings.")
                print("\nWARNING: Existing database found without a configuration snapshot.")
                print(">>> Proceeding with CURRENT configuration settings. <<<")
                if not db_manager.save_initial_config(current_target_album_key, current_target_album_uri, current_target_folder_path, current_google_album_arg):
                     logger.error("Failed to save current configuration as snapshot.")


        # --- Reset Errors if Requested ---
        if args.reset_errors:
             db_manager.reset_failed_items()
             total_db_items = db_manager.get_item_count()


        # --- Get Items to Process from Database ---
        logger.info("Fetching items to process from database...")
        items_to_process = db_manager.get_items_to_process(retry_errors=args.retry_errors)
        total_items_to_process = len(items_to_process)
        logger.info(f"Found {total_items_to_process} items requiring processing.")
        if total_items_to_process == 0 and not is_initial_run: # Check if it was just an initial run
             logger.info("No items require processing based on current database state and flags.")
             print("No items require processing.")
             # Proceed to final summary and potential rename
        elif total_items_to_process == 0 and is_initial_run:
             logger.info("Initial run completed, but no items needed processing (e.g., all filtered).")
             print("Initial run completed, no items required processing.")
             # Proceed to final summary and potential rename


        # --- Process Items from DB ---
        processed_in_run = 0; uploaded_in_run = 0; duplicates_in_run = 0; skipped_in_run = 0; errors_in_run = 0

        # Only loop if there are items to process
        if total_items_to_process > 0:
            for item_index, item_row in enumerate(items_to_process):
                current_item_number_in_batch = item_index + 1
                if shutdown_requested:
                    logger.warning(f"Shutdown requested. Stopping processing loop.")
                    break

                google_id = item_row['google_id']
                filename = item_row['filename']
                mime_type = item_row['mime_type']
                current_status = item_row['status']
                db_md5_hash = item_row['md5_hash']
                item_details_for_download = dict(item_row)

                is_video = mime_type.startswith('video/')
                item_type = "Video" if is_video else "Photo"

                truncated_id = f"{google_id[:LOG_ID_TRUNCATE_LEN]}...{google_id[-LOG_ID_TRUNCATE_LEN:]}" if len(google_id) > LOG_ID_TRUNCATE_LEN * 2 else google_id
                log_identifier = f"Item {current_item_number_in_batch}/{total_items_to_process} (ID: {truncated_id}, File: '{filename}', Type: {item_type}, Status: {current_status})"

                logger.info("-" * 50)
                logger.info(f"Processing {log_identifier}")
                print("-" * 30)
                print(f"-> Processing {current_item_number_in_batch}/{total_items_to_process}: {filename} ({item_type}) [Status: {current_status}]")

                processed_in_run += 1
                temp_file_path = None
                current_md5_hash = db_md5_hash
                refreshed_details = None

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
                    temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details_for_download)

                    if not temp_file_path:
                        logger.error(f"{log_identifier}: Download failed for MD5 check.")
                        db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, error_message="Download failed during hash check")
                        errors_in_run += 1
                        skipped_in_run += 1
                        continue

                    if refreshed_details:
                         new_base_url = refreshed_details.get('baseUrl')
                         new_metadata_json = json.dumps(refreshed_details.get('mediaMetadata', {}))
                         db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                         refreshed_details = None

                    logger.info(f"{log_identifier}: Calculating MD5 hash...")
                    print("   Calculating MD5 hash...")
                    calculated_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')

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
                          if not target_album_key_for_check:
                               logger.error(f"{log_identifier}: SmugMug target album key missing. Cannot check existence.")
                               db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, error_message="SM album key missing during check")
                               errors_in_run += 1; skipped_in_run += 1
                               continue

                          logger.info(f"{log_identifier}: Checking SmugMug for duplicates...")
                          print("   Checking SmugMug for duplicates...")
                          logger.debug(f"{log_identifier}: Checking existence on SmugMug album key: {target_album_key_for_check}")

                          if is_video:
                               log_reason = "filename match"
                               exists_on_smugmug = smugmug.check_media_exists(target_album_key_for_check, filename, mime_type)
                          elif not is_video and not is_heic:
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
                     temp_file_path, _, _, refreshed_details = google_photos.download_photo(item_details_for_download)

                     if not temp_file_path:
                          logger.error(f"{log_identifier}: Download failed before upload.")
                          db_manager.update_item_status(google_id, STATUS_ERROR_DOWNLOAD, error_message="Download failed before upload")
                          errors_in_run += 1
                          skipped_in_run += 1
                          continue

                     if refreshed_details:
                          new_base_url = refreshed_details.get('baseUrl')
                          new_metadata_json = json.dumps(refreshed_details.get('mediaMetadata', {}))
                          db_manager.update_item_details(google_id, new_base_url, new_metadata_json)
                          refreshed_details = None
                else:
                     logger.debug(f"{log_identifier}: Using existing local file for upload: {temp_file_path}")


                # --- Perform Upload ---
                target_album_uri_for_upload = smugmug.album_api_uri
                if not target_album_uri_for_upload:
                     logger.error(f"{log_identifier}: SmugMug target album URI missing. Cannot upload.")
                     db_manager.update_item_status(google_id, STATUS_ERROR_SMUGMUG_API, error_message="SM album URI missing during upload")
                     errors_in_run += 1; skipped_in_run += 1
                     continue

                logger.info(f"{log_identifier}: Uploading to SmugMug URI: {target_album_uri_for_upload}...")
                print(f"   Uploading to SmugMug...")
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
        if total_items_to_process > 0: # Only log end of batch if we processed items
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

        # --- Determine Final Status and Handle DB Rename ---
        run_completed_successfully = (errors_in_run == 0 and not shutdown_requested)

        if run_completed_successfully:
            print("- Status: Completed run successfully")
            # Check if all items in the DB are accounted for (terminal status)
            pending_items = db_manager.get_items_to_process(retry_errors=False) # Check for non-error pending
            if not pending_items:
                logger.info("All items processed successfully. Run complete.")
                print("All items processed successfully.")
                # --- Rename DB on Success ---
                logger.info("Attempting to rename completed database file...")
                # Close DB connection FIRST
                if db_manager_global:
                    db_manager_global.close()
                    logger.info("Closed database connection before renaming.")
                else:
                    logger.warning("DB manager instance not found, cannot close before rename.")

                # Construct new name
                base_db_name, db_ext = os.path.splitext(args.db_file)
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                new_db_filename = f"{base_db_name}_completed_{timestamp}{db_ext}"

                try:
                    if os.path.exists(args.db_file):
                        os.rename(args.db_file, new_db_filename)
                        logger.info(f"Successfully renamed database to: {new_db_filename}")
                        print(f"Database renamed to: {new_db_filename}")
                        # Prevent cleanup from trying to close DB again or release lock
                        cleanup_handled_in_try = True
                        # Manually release lock as cleanup won't run fully now
                        release_lock()
                    else:
                        logger.warning(f"Database file {args.db_file} not found for renaming (already closed/renamed?).")

                except OSError as e:
                    logger.error(f"Failed to rename database file from {args.db_file} to {new_db_filename}: {e}", exc_info=True)
                    print(f"\nERROR: Failed to rename completed database file: {e}")
                    # Allow normal cleanup to proceed in finally block
            else:
                 logger.warning(f"Run completed without errors, but {len(pending_items)} items still require processing. DB not renamed.")
                 print(f"Run completed without errors, but {len(pending_items)} items still require processing. Rerun to continue.")

        elif shutdown_requested:
            print("- Status: Terminated by user")
        else: # errors_in_run > 0
            print(f"- Status: Completed run with {errors_in_run} errors")

        print(f"- Run Time: {total_duration:.2f} sec")
        print("-" * 60)
        print("Overall Database Stats:")
        for status, count in sorted(final_stats.items()):
             if count > 0: print(f"- {status}: {count}")
        print(f"- Detailed Log: {LOG_FILE}")
        if run_completed_successfully and not pending_items:
             print(f"- Completed Database File: {new_db_filename}")
        else:
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
        # Only run full cleanup if it wasn't handled successfully in the try block
        if not cleanup_handled_in_try:
            cleanup(google_photos_instance_global, db_manager_global)

        # Determine exit code based on errors *in this run*
        exit_code = 1 if errors_in_run > 0 or shutdown_requested else 0
        logger.info(f"Exiting script with code {exit_code}.")
        sys.exit(exit_code)

if __name__ == "__main__":
    # __version__ remains "1.9" as requested
    main()
