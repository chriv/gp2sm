# Google Photos to SmugMug Transfer Script
#
# This script facilitates transferring media from Google Photos to SmugMug.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#
__version__ = "1.4"  # Updated version number for logging changes

# Standard library imports
import argparse
import json                 # Needed for JSON checks
import logging
import os
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler

# Third-party imports
import colorlog

# Local module imports
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug

# --- TODO List ---
# DONE: Clean up temp_downloads (handled by google_photos_module.__del__)
# DONE: Provide more output when handling batches of Google Photos files (added progress indicators)
# DONE: Ignore HEIC files by default (override in config and/or command line)
# DONE: Warn users about HEIC files being converted to JPEGS (and flattened) by SmugMug
# DONE: Don't duplicate check HEIC files at all if being processed (no possible way)
# DONE: (Deferred - Not Feasible) Get MD5 hashes from Google Photos API before downloading.
# DONE: Generate a smugmug_config.json file with placeholders and comments if it doesn't exist
# DONE: If script terminates from missing config file requirements, add the missing placeholders and comments to the config
# DONE: Instruct user how to get google_api_keys.json if missing
# DONE: Colorize logging output
# DONE: Implement file logging
# TODO: Fix Google Photos Album selection (programmatically get Album ID from name?) - Low Priority
# TODO: There appear to be local variables with the same purpose as attributes in the SmugMug class. Refactor for clarity.
# TODO: Review SmugMug folder/album creation logic for robustness, especially edge cases with existing names/paths.
# TODO: Add more comprehensive error handling around API calls (rate limits, specific HTTP errors).
# TODO: Consider adding option to specify start/end dates for Google Photos items.
# TODO: PEP 8 compliance review.

# --- Logging Setup ---

# Define custom SUCCESS level (between INFO and WARNING)
SUCCESS_LEVEL_NUM = 25
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")

def log_success(self, message, *args, **kws):
    # Add method to logger class
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        self._log(SUCCESS_LEVEL_NUM, message, args, **kws)
logging.Logger.success = log_success

# Define format strings
LOG_FORMAT_CONSOLE = (
    '%(log_color)s%(asctime)s - %(levelname)-8s - '
    '[%(module)s:%(lineno)d] - %(message)s%(reset)s'
)
LOG_FORMAT_FILE = (
    '%(asctime)s - %(levelname)-8s - [%(name)s:%(module)s:%(lineno)d] - %(message)s'
)

# Get the root logger
logger = logging.getLogger()  # Get the root logger (important!)

# --- Setup Colored Console Handler ---
console_formatter = colorlog.ColoredFormatter(
    LOG_FORMAT_CONSOLE,
    datefmt='%Y-%m-%d %H:%M:%S',
    reset=True,
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'white',
        'SUCCESS': 'green',  # Custom level color
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'red,bg_white',
    },
    secondary_log_colors={},
    style='%'
)
console_handler = colorlog.StreamHandler(sys.stdout)  # Use colorlog's handler
console_handler.setFormatter(console_formatter)

# --- Setup Rotating File Handler ---
LOG_FILE = "gp2sm_transfer.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3  # Keep 3 backup log files

file_formatter = logging.Formatter(LOG_FORMAT_FILE, datefmt='%Y-%m-%d %H:%M:%S')
try:
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'  # Explicitly set encoding
    )
    file_handler.setFormatter(file_formatter)
    # --- Add Handlers to Root Logger ---
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
except Exception as e:
    # If file handler setup fails, log to console only
    logger.addHandler(console_handler)  # Ensure console handler is added
    logger.error(f"!!! Failed to set up file logging to {LOG_FILE}: {e} !!!")
    logger.error("!!! Log messages will only be shown on the console. !!!")

# Set initial logging level (will be updated by --debug flag later)
logger.setLevel(logging.INFO)

# --- End of Logging Setup ---

# --- Configuration File Constants ---
GOOGLE_PHOTOS_CREDENTIALS_FILE = 'google_api_keys.json'
SMUGMUG_CONFIG_FILE = 'smugmug_config.json'
GOOGLE_PHOTOS_TOKEN_FILE = 'google_photos_token.json'

# --- Script Settings ---
BATCH_SIZE = 50  # Number of photos to process in each Google Photos API call
LOCK_FILE = "gp2sm.lock"  # File used to prevent multiple instances

# Global variable for lock file handle
lock_file_handle = None

def acquire_lock():
    """Tries to acquire an exclusive lock file."""
    global lock_file_handle
    try:
        # Use 'w' mode to truncate/overwrite if it exists but wasn't locked
        # Use os.O_CREAT | os.O_EXCL for atomic creation check if available/needed,
        # but simple 'x' mode is often sufficient. Let's stick to 'x'.
        lock_file_handle = open(LOCK_FILE, "x")
        logging.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        logging.error(f"Lock file '{LOCK_FILE}' exists. Another instance might be running.")
        print(f"Error: Lock file '{LOCK_FILE}' found. Is another instance running?")
        print("If not, please manually delete the lock file and try again.")
        return False
    except PermissionError:
        logging.error(f"Permission denied when trying to create lock file: {LOCK_FILE}")
        print(f"Error: Could not create lock file '{LOCK_FILE}' due to permission issues.")
        return False
    except Exception as e:
        logging.error(f"An error occurred trying to create lock file '{LOCK_FILE}': {e}", exc_info=True)
        print(f"Error: Could not create lock file '{LOCK_FILE}'. Check permissions or other errors in log.")
        return False


def release_lock():
    """Releases the lock file."""
    global lock_file_handle
    if lock_file_handle:
        try:
            lock_file_handle.close()
            lock_file_handle = None  # Reset handle
            # Only remove the file if we successfully closed it
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
            logging.info(f"Released lock file: {LOCK_FILE}")
        except Exception as e:
            logging.error(f"Error releasing lock file '{LOCK_FILE}': {e}")
            # Handle might be None if lock wasn't acquired but cleanup is called
    elif os.path.exists(LOCK_FILE):
        # If handle is None but file exists, maybe script crashed before release?
        logging.warning(f"Lock file handle was None, but lock file '{LOCK_FILE}' exists. Attempting removal.")
        try:
            os.remove(LOCK_FILE)
            logging.info(f"Removed potentially orphaned lock file: {LOCK_FILE}")
        except Exception as e:
            logging.error(f"Error removing orphaned lock file '{LOCK_FILE}': {e}")


def cleanup(google_photos_instance):
    """
    Cleans up temporary resources and releases the lock file.
    This function should be called in a 'finally' block to ensure it runs.
    """
    logging.info("Running cleanup...")

    # Clean up Google Photos temporary directory (uses its __del__ method)
    if google_photos_instance:
        try:
            del google_photos_instance  # Trigger __del__ explicitly if object exists
        except Exception as e:
            logging.warning(f"Error during Google Photos cleanup: {e}")

    # Release the lock file
    release_lock()
    logging.info("Cleanup complete.")


def main():
    """Main function to orchestrate the photo transfer."""

    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(
        description="Transfer photos and videos from Google Photos to SmugMug.",
        epilog="Example: python main.py --smugmug-album \"Google Photos Import\" --smugmug-folder \"Vacations/2024\" --process-heic"
    )
    parser.add_argument('--delete-from-google', action='store_true',
                        help='[SIMULATED] Ask to delete from Google Photos after successful SmugMug check/upload (API currently does NOT support deletion).')
    parser.add_argument('--google-photos-album-id', type=str,
                        help='Process photos only from the specified Google Photos album ID.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Perform a dry run: check existence, log actions, but do not upload to SmugMug or simulate deletion.')
    parser.add_argument('--ignore-photos', action='store_true',
                        help='Skip processing media items identified as photos (images).')
    parser.add_argument('--ignore-videos', action='store_true',
                        help='Skip processing media items identified as videos.')
    parser.add_argument('--smugmug-album', type=str,
                        help='Name of the target SmugMug album. Overrides config file setting. If album/path does not exist, it will be created.')
    parser.add_argument('--smugmug-folder', type=str,
                        help='Path of SmugMug folders (e.g., "Folder/SubFolder"). Overrides config file setting. If path does not exist, it will be created.')
    parser.add_argument('--process-heic', action='store_true',
                        help='Process HEIC files (Live Photos). Default is to ignore them. WARNING: SmugMug converts these to JPGs, losing the live aspect, and duplicate checking is disabled.')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging (both console and file).')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')

    args = parser.parse_args()

    # --- Update Logging Level Based on Arg ---
    # *AFTER* parsing args, set the level for all handlers
    if args.debug:
        logger.setLevel(logging.DEBUG)
        # Set level for individual handlers too if needed, but root logger level usually suffices
        # console_handler.setLevel(logging.DEBUG)
        # file_handler.setLevel(logging.DEBUG)
        logging.debug("Debug logging enabled.")  # This will now be cyan
    else:
        logger.setLevel(logging.INFO)  # Keep INFO level if --debug is not set

    # --- Acquire Lock File ---
    if not acquire_lock():
        sys.exit(1)  # Exit if lock cannot be acquired

    google_photos = None  # Initialize to None for cleanup context
    try:
        # --- SmugMug Initialization & Configuration ---
        logging.info(f"Initializing SmugMug using config: {SMUGMUG_CONFIG_FILE}")
        smugmug = SmugMug(SMUGMUG_CONFIG_FILE)
        try:
            if not smugmug.load_config():
                # Error during loading (e.g., JSON decode)
                logging.error(
                    f"Failed to load or parse SmugMug config '{SMUGMUG_CONFIG_FILE}'. Please check the file format.")
                print(f"\nError: Failed to load or parse '{SMUGMUG_CONFIG_FILE}'. Is it valid JSON?")
                sys.exit(1)
        except FileNotFoundError:
            # Config file missing, generate default and exit
            logging.warning(f"SmugMug config file '{SMUGMUG_CONFIG_FILE}' not found.")
            if not smugmug.generate_default_config():
                # Failed to generate default (e.g., permission error)
                logging.critical("Failed to generate the default SmugMug config file.")
            # Exit after generating or failing to generate
            sys.exit(1)
        except Exception as e:
            logging.critical(f"Unexpected error loading SmugMug config: {e}", exc_info=True)
            sys.exit(1)

        # --- Google Photos Initialization & Configuration ---
        logging.info(f"Initializing Google Photos using credentials: {GOOGLE_PHOTOS_CREDENTIALS_FILE}")
        try:
            google_photos = GooglePhotos(GOOGLE_PHOTOS_CREDENTIALS_FILE, GOOGLE_PHOTOS_TOKEN_FILE, BATCH_SIZE)
            # Authentication happens within __init__ -> authenticate
            # is_authenticated() check is crucial here after potential errors during init
            if not google_photos.is_authenticated():
                logging.error("Google Photos client initialized but failed to authenticate. Check logs.")
                print("\nError: Could not authenticate with Google Photos. Please check log messages above.")
                sys.exit(1)
            logger.success("Google Photos client initialized and authenticated.")

        except GoogleCredentialsNotFoundError:
            # Specific error message already logged/printed by google_photos_module
            logging.info("Exiting due to missing Google Photos credentials file.")
            # Message printed by module, exit needed here.
            sys.exit(1)
        except Exception as e:
            # Catch other potential errors during Google Photos init/auth
            logging.critical(f"An critical unexpected error occurred during Google Photos initialization: {e}",
                             exc_info=True)
            print(f"\nAn critical unexpected error occurred during Google Photos setup: {e}")
            sys.exit(1)

        # --- SmugMug Authentication & Configuration Check ---
        logging.info("Performing SmugMug configuration check and authentication...")
        if not smugmug.check_config_and_authenticate():
            logging.error("SmugMug configuration check or authentication failed.")
            # Specific messages should have been logged/printed by check_config_and_authenticate
            print(
                f"\nError: SmugMug setup failed. Please check '{SMUGMUG_CONFIG_FILE}' and log messages, then run again.")
            sys.exit(1)
        logger.success("SmugMug client initialized and authenticated.")

        # --- Determine Final Album/Folder Configuration ---
        # Start with values potentially set during check_config_and_authenticate
        smugmug_album_name = smugmug.album_name
        smugmug_folder_path_str = smugmug.folder_name  # Use the attribute name directly
        # Note: smugmug.album_key / album_api_uri are also set if key was used in config

        # Override with command-line args if provided
        if args.smugmug_album:
            smugmug_album_name = args.smugmug_album
            logging.info(f"Using SmugMug album name from command line: '{smugmug_album_name}'")
            # If album name is specified via CLI, it takes precedence over any key/uri from config
            smugmug.album_key = None
            smugmug.album_api_uri = None
        elif not smugmug_album_name and smugmug.album_key:
            # If CLI didn't specify name, and config had key/uri, log that
            logging.info(f"Using SmugMug album key from config: '{smugmug.album_key}'")

        if args.smugmug_folder:
            smugmug_folder_path_str = args.smugmug_folder
            logging.info(f"Using SmugMug folder path from command line: '{smugmug_folder_path_str}'")
        elif smugmug_folder_path_str:  # Log folder from config if not overridden
            logging.info(f"Using SmugMug folder path from config: '{smugmug_folder_path_str}'")

        # --- Ensure Album Exists on SmugMug ---
        # This requires either name or key/uri to be definitively set
        if not smugmug_album_name and not smugmug.album_key:
            logging.critical("No SmugMug album specified in config or via command line after checks.")
            print(
                "\nError: No target SmugMug album could be determined. Please configure 'album_name' or 'album_key'/'album_api_uri'.")
            sys.exit(1)

        # Use get_or_create_album_in_path which handles folder creation and album get/create
        logging.info(
            f"Ensuring SmugMug album '{smugmug_album_name}' exists in path '{smugmug_folder_path_str or 'Root'}'.")
        if not smugmug.get_or_create_album_in_path(smugmug_album_name, smugmug_folder_path_str):
            logging.critical(
                f"Failed to find or create the target SmugMug album '{smugmug_album_name}' in path '{smugmug_folder_path_str or 'Root'}'. Exiting.")
            print(
                f"\nError: Could not ensure SmugMug album '{smugmug_album_name}' exists. Check logs and SmugMug permissions.")
            sys.exit(1)

        # At this point, smugmug.album_key and smugmug.album_api_uri should be set correctly
        target_album_key = smugmug.album_key
        target_album_api_uri = smugmug.album_api_uri
        logger.success(
            f"Confirmed target SmugMug album. Name: '{smugmug.album_name}', Key: {target_album_key}, URI: {target_album_api_uri}")

        # --- Process HEIC Flag ---
        # Read final value from config (might have been added by check_config_and_authenticate)
        process_heic_config = smugmug.config.get('process_heic', False)
        process_heic_enabled = args.process_heic or process_heic_config  # Command line overrides config
        if process_heic_enabled:
            logging.warning("--- HEIC Processing Enabled ---")
            logging.warning("SmugMug converts HEIC files (including Live Photo video) into static JPGs.")
            logging.warning("The 'live' photo aspect will be lost on SmugMug.")
            logging.warning("Duplicate checking for HEIC files is DISABLED.")
            # No need for print statement here, warning logs cover it

        # --- Get Media Items from Google Photos ---
        logging.info("Fetching media items from Google Photos...")
        photos = google_photos.get_photos(args.google_photos_album_id)
        total_items = len(photos)
        if total_items == 0:
            logging.info("No media items found in the specified Google Photos location. Nothing to transfer.")
            logger.success("Script finished successfully (no items to transfer).")
            sys.exit(0)  # Successful exit, nothing to do

        logging.info(f"Found {total_items} items in Google Photos.")
        processed_count = 0
        skipped_count = 0
        uploaded_count = 0
        duplicate_count = 0
        error_count = 0

        logging.info(
            f"Starting transfer process... (Dry Run: {args.dry_run}, Ignore Photos: {args.ignore_photos}, Ignore Videos: {args.ignore_videos})")
        print("-" * 60)  # Console separator

        # --- Main Processing Loop ---
        start_time = time.time()
        for item_index, item in enumerate(photos):
            processed_count += 1
            filename = item.get('filename')
            media_item_id = item['id']
            mime_type = item.get('mimeType', '')
            product_url = item.get('productUrl', '#')  # URL to view on Google Photos

            # Basic item validation
            if not filename or not mime_type:
                logging.warning(
                    f"Skipping item {item_index + 1}/{total_items} (ID: {media_item_id}, URL: {product_url}) due to missing filename or mimeType.")
                skipped_count += 1
                error_count += 1
                continue

            # --- Check File Type and Flags ---
            _, file_extension = os.path.splitext(filename)
            is_heic = file_extension.lower() == '.heic'
            is_video = mime_type.startswith('video/')
            item_type = "Video" if is_video else "Photo"

            # Skip based on flags
            if is_heic and not process_heic_enabled:
                logging.info(
                    f"Skipping {item_index + 1}/{total_items}: HEIC file '{filename}' (ID: {media_item_id}) as processing is disabled.")
                skipped_count += 1
                continue
            if args.ignore_photos and not is_video:
                logging.info(
                    f"Skipping {item_index + 1}/{total_items}: Photo '{filename}' (ID: {media_item_id}) due to --ignore-photos flag.")
                skipped_count += 1
                continue
            if args.ignore_videos and is_video:
                logging.info(
                    f"Skipping {item_index + 1}/{total_items}: Video '{filename}' (ID: {media_item_id}) due to --ignore-videos flag.")
                skipped_count += 1
                continue

            logging.info(
                f"Processing {item_index + 1}/{total_items} - {item_type}: '{filename}' (ID: {media_item_id}){' (HEIC)' if is_heic else ''}")
            logging.debug(f"  MimeType: {mime_type}, Google URL: {product_url}")
            print(
                f"-> Processing {item_index + 1}/{total_items}: {filename} ({item_type})")  # Progress indicator for console

            # --- Existence Check on SmugMug ---
            exists_on_smugmug = False
            temp_file_path = None  # Path to downloaded file, if needed
            google_file_hash = None  # MD5 hash from Google (not available) or calculated locally

            if is_heic and process_heic_enabled:
                logging.info(f"  Duplicate check skipped for HEIC file '{filename}'.")
                # Treat as "does not exist" to proceed with download/upload
            else:
                # Perform duplicate check for non-HEIC files
                logging.debug(f"  Checking existence on SmugMug for '{filename}'...")
                check_start_time = time.time()
                if is_video:
                    # Check videos by filename only
                    exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type)
                    log_reason = "filename match" if exists_on_smugmug else "filename not found"
                else:
                    # Check images by MD5 hash - requires download first
                    logging.debug(f"  Downloading image '{filename}' for MD5 check...")
                    temp_file_path, _, _, _, _ = google_photos.download_photo(item)  # Use instance temp dir

                    if not temp_file_path:
                        logging.error(f"  Skipping image '{filename}' due to download error for hash check.")
                        skipped_count += 1
                        error_count += 1
                        continue  # Skip to next item

                    # Calculate MD5 hash locally
                    google_file_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
                    if not google_file_hash:
                        logging.error(f"  Skipping image '{filename}' due to MD5 hash calculation error.")
                        if os.path.exists(temp_file_path):
                            try:
                                os.remove(temp_file_path)
                            except OSError:
                                pass
                        skipped_count += 1
                        error_count += 1
                        continue  # Skip to next item

                    logging.debug(f"  Calculated MD5 for '{filename}': {google_file_hash}. Checking SmugMug...")
                    # Check SmugMug for image existence by MD5 hash
                    exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type,
                                                                   file_hash=google_file_hash)
                    log_reason = f"matching MD5 hash: {google_file_hash}" if exists_on_smugmug else f"MD5 hash {google_file_hash} not found"

                check_duration = time.time() - check_start_time
                logging.debug(f"  Existence check took {check_duration:.2f} seconds.")

                if exists_on_smugmug:
                    logging.info(f"  FOUND on SmugMug ({log_reason}).")
                    print(f"   Exists on SmugMug ({'Filename' if is_video else 'MD5 Hash'}). Skipping.")
                    duplicate_count += 1
                    # Clean up downloaded file *now* if it already exists (only images were downloaded)
                    if temp_file_path and os.path.exists(temp_file_path):
                        try:
                            os.remove(temp_file_path)
                            logging.debug(f"  Removed temp file {temp_file_path} for duplicate item.")
                            temp_file_path = None  # Clear path variable
                        except Exception as e:
                            logging.warning(f"  Could not remove temp file {temp_file_path} after duplicate check: {e}")
                else:
                    logging.info(f"  Not found on SmugMug ({log_reason}). Will proceed.")
                    # Keep temp_file_path if it was downloaded for image hash check

            # --- Process Based on Existence ---
            if exists_on_smugmug:
                # Handle optional deletion from Google Photos (currently simulated)
                if args.delete_from_google:
                    if args.dry_run:
                        logging.info(
                            f"  [DRY RUN] Would simulate removal of '{filename}' (ID: {media_item_id}) from Google Photos (as it exists on SmugMug).")
                        # google_photos.remove_photo(media_item_id, dry_run=True) # Simulate
                    else:
                        # Actual deletion is not supported by API
                        logging.warning(
                            f"  Skipping deletion of '{filename}' (ID: {media_item_id}) from Google Photos - API does not support.")
                # Continue to next item in the loop
                continue

            # --- Item Does Not Exist on SmugMug (or is HEIC) ---
            # Need to download if not already downloaded
            if not temp_file_path or not os.path.exists(temp_file_path):
                logging.debug(f"  Downloading {'video' if is_video else 'HEIC/image'} '{filename}' for upload...")
                dl_start_time = time.time()
                temp_file_path, _, _, _, downloaded_mime_type = google_photos.download_photo(item)
                dl_duration = time.time() - dl_start_time

                if not temp_file_path:
                    logging.error(f"  Skipping '{filename}' due to download error during upload phase.")
                    print(f"   ERROR downloading '{filename}'. Skipping.")
                    skipped_count += 1
                    error_count += 1
                    continue  # Skip to next item
                else:
                    logging.debug(f"  Download took {dl_duration:.2f} seconds.")
                    # Use the mime type reported by the download if available, otherwise stick to original
                    if downloaded_mime_type: mime_type = downloaded_mime_type

            # --- Upload to SmugMug ---
            if args.dry_run:
                logging.info(
                    f"  [DRY RUN] Would upload '{filename}' ({item_type}) from {temp_file_path} to SmugMug album URI: {target_album_api_uri}")
                print(f"   [DRY RUN] Would upload '{filename}'.")
                # In dry run, clean up the temp file now (upload_media usually does this)
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        logging.debug(f"  [DRY RUN] Removed temporary file: {temp_file_path}")
                    except Exception as e:
                        logging.warning(f"  [DRY RUN] Could not remove temporary file {temp_file_path}: {e}")
            else:
                # Perform the actual upload
                logging.info(f"  Uploading '{filename}' ({item_type}, {mime_type}) to SmugMug...")
                print(f"   Uploading '{filename}' to SmugMug...")
                upload_start_time = time.time()
                # upload_media now handles its own temp file cleanup
                if smugmug.upload_media(target_album_api_uri, temp_file_path, filename, mime_type):
                    upload_duration = time.time() - upload_start_time
                    # Use SUCCESS level for successful uploads
                    logger.success(f"  Successfully uploaded '{filename}' to SmugMug (took {upload_duration:.2f}s).")
                    print(f"   Successfully uploaded '{filename}'.")
                    uploaded_count += 1
                    # Deletion after successful upload (simulated)
                    if args.delete_from_google:
                        logging.warning(
                            f"  Skipping deletion of '{filename}' (ID: {media_item_id}) from Google Photos after upload - API does not support.")
                else:
                    upload_duration = time.time() - upload_start_time
                    logging.error(
                        f"  Failed to transfer '{filename}' to SmugMug (took {upload_duration:.2f}s). Check logs for details.")
                    print(f"   ERROR uploading '{filename}'. See log for details.")
                    error_count += 1
                    # Temp file is cleaned up inside upload_media's finally block

        # --- End of Loop ---
        end_time = time.time()
        total_duration = end_time - start_time
        logging.info("-" * 50)
        logging.info("Transfer Process Summary:")
        logging.info(f"  Total items retrieved from Google Photos: {total_items}")
        logging.info(f"  Items processed: {processed_count}")
        logger.success(f"  Successfully uploaded: {uploaded_count}")  # Green
        logging.info(f"  Found as duplicates on SmugMug: {duplicate_count}")  # White
        logging.info(f"  Skipped by flags/settings: {skipped_count}")  # White
        if error_count > 0:
            logging.error(f"  Errors encountered (download/upload/hash): {error_count}")  # Red
        else:
            logging.info(f"  Errors encountered: {error_count}")  # White if 0 errors
        logging.info(f"  Dry Run mode: {args.dry_run}")  # White
        logging.info(f"  Deletion requested: {args.delete_from_google} (Simulated - API Unsupported)")  # White
        logging.info(f"  Total execution time: {total_duration:.2f} seconds")  # White
        logging.info("-" * 50)
        if error_count == 0 and processed_count == total_items:
            logger.success("Script finished successfully.")  # Green
        elif error_count > 0:
            logging.warning(f"Script finished with {error_count} errors. Please review logs.")  # Yellow
        else:
            logging.warning("Script finished, but some items may have been skipped unexpectedly.")  # Yellow

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
        logging.warning("Keyboard interrupt detected. Shutting down gracefully...")
        print("\nKeyboard interrupt received. Cleaning up...")
        # Allow finally block to run
    except Exception as e:
        # Catch any unexpected errors in the main block
        logging.critical(f"An critical unexpected error occurred in the main processing loop: {e}", exc_info=True)
        print(f"\nAn critical unexpected error occurred: {e}. Check the log file '{LOG_FILE}' for details.")
    finally:
        # Ensure cleanup is always called, passing the google_photos object
        cleanup(google_photos)


if __name__ == "__main__":
    main()
