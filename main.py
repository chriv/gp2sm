# Google Photos to SmugMug Transfer Script
#
# This script facilitates transferring media from Google Photos to SmugMug.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#
__version__ = "1.3"  # Updated version number

# Standard library imports
import argparse
import logging
import os
import shutil
import sys
import time
import json  # Needed for JSON checks

# Local module imports
# Add custom exception import
from google_photos_module import GooglePhotos, GoogleCredentialsNotFoundError
from smugmug_module import SmugMug

# Third-party imports (currently none, but placeholder)

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
# TODO: Colorize logging output
# TODO: Implement file logging
# TODO: Fix Google Photos Album selection (programmatically get Album ID from name?) - Low Priority
# TODO: There appear to be local variables with the same purpose as attributes in the SmugMug class. Refactor for clarity.
# TODO: Review SmugMug folder/album creation logic for robustness, especially edge cases with existing names/paths.
# TODO: Update README.md with latest options, setup steps, and HEIC/deletion details.
# TODO: Update smugmug_config.json.example.txt to match DEFAULT_SMUGMUG_CONFIG.
# TODO: Add more comprehensive error handling around API calls (rate limits, specific HTTP errors).
# TODO: Consider adding option to specify start/end dates for Google Photos items.
# TODO: PEP 8 compliance review.
# TODO: Clean up imports.

# Configure logging
# Consider adding a file handler as well
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s')
log_handler = logging.StreamHandler(sys.stdout)  # Log to console
log_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)  # Set default level
logger.addHandler(log_handler)

# Optional: Add file logging
# file_handler = logging.FileHandler("gp2sm_transfer.log")
# file_handler.setFormatter(log_formatter)
# logger.addHandler(file_handler)


# --- Configuration File Constants ---
# Use constants for filenames
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
        lock_file_handle = open(LOCK_FILE, "x")
        logging.info(f"Acquired lock file: {LOCK_FILE}")
        return True
    except FileExistsError:
        logging.error(f"Lock file '{LOCK_FILE}' exists. Another instance might be running.")
        print(f"Error: Lock file '{LOCK_FILE}' found. Is another instance running?")
        print("If not, please manually delete the lock file and try again.")
        return False
    except Exception as e:
        logging.error(f"An error occurred trying to create lock file '{LOCK_FILE}': {e}")
        print(f"Error: Could not create lock file '{LOCK_FILE}'. Check permissions.")
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
            logging.info(f"Removed orphaned lock file: {LOCK_FILE}")
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
        del google_photos_instance  # Trigger __del__ explicitly if object exists

    # Release the lock file
    release_lock()
    logging.info("Cleanup complete.")


def main():
    """Main function to orchestrate the photo transfer."""

    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(description="Transfer photos from Google Photos to SmugMug.")
    parser.add_argument('--delete-from-google', action='store_true',
                        help='[UNSUPPORTED] Ask to delete from Google Photos after successful SmugMug check/upload (API does not support deletion).')
    parser.add_argument('--google-photos-album-id', type=str,
                        help='Process photos only from the specified Google Photos album ID.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Perform a dry run: check existence, log actions, but do not upload to SmugMug or delete from Google Photos.')
    parser.add_argument('--ignore-photos', action='store_true',
                        help='Skip processing media items identified as photos (images).')
    parser.add_argument('--ignore-videos', action='store_true',
                        help='Skip processing media items identified as videos.')
    parser.add_argument('--smugmug-album', type=str,
                        help='Name of the SmugMug album. Overrides config file setting. If album/path does not exist, it will be created.')
    parser.add_argument('--smugmug-folder', type=str,
                        help='Path of SmugMug folders (e.g., "Folder/SubFolder"). Overrides config file setting. If path does not exist, it will be created.')
    parser.add_argument('--process-heic', action='store_true',
                        help='Process HEIC files (Live Photos). Default is to ignore them. WARNING: SmugMug converts these to JPGs, losing the live aspect, and duplicate checking is disabled.')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging.')

    args = parser.parse_args()

    # --- Setup Logging Level ---
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.debug("Debug logging enabled.")
    else:
        logger.setLevel(logging.INFO)

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
                print(f"Error: Failed to load or parse '{SMUGMUG_CONFIG_FILE}'. Is it valid JSON?")
                sys.exit(1)
        except FileNotFoundError:
            # Config file missing, generate default and exit
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
                print("Error: Could not authenticate with Google Photos. Please check log messages above.")
                sys.exit(1)

        except GoogleCredentialsNotFoundError:
            # Specific error message already logged/printed by google_photos_module
            logging.info("Exiting due to missing Google Photos credentials file.")
            sys.exit(1)
        except Exception as e:
            # Catch other potential errors during Google Photos init/auth
            logging.critical(f"An unexpected error occurred during Google Photos initialization: {e}", exc_info=True)
            print(f"An critical unexpected error occurred during Google Photos setup: {e}")
            sys.exit(1)

        # --- SmugMug Authentication & Configuration Check ---
        logging.info("Performing SmugMug configuration check and authentication...")
        if not smugmug.check_config_and_authenticate():
            logging.error("SmugMug configuration check or authentication failed.")
            # Specific messages should have been logged/printed by check_config_and_authenticate
            # Ensure user knows to check the config file and logs.
            print(
                f"\nError: SmugMug setup failed. Please check '{SMUGMUG_CONFIG_FILE}' and log messages, then run again.")
            sys.exit(1)

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
        if not smugmug.get_or_create_album_in_path(smugmug_album_name, smugmug_folder_path_str):
            logging.critical(
                f"Failed to find or create the target SmugMug album '{smugmug_album_name}' in path '{smugmug_folder_path_str or 'root'}'. Exiting.")
            print(
                f"\nError: Could not ensure SmugMug album '{smugmug_album_name}' exists. Check logs and SmugMug permissions.")
            sys.exit(1)

        # At this point, smugmug.album_key and smugmug.album_api_uri should be set correctly
        target_album_key = smugmug.album_key
        target_album_api_uri = smugmug.album_api_uri
        logging.info(f"Confirmed target SmugMug album. Key: {target_album_key}, URI: {target_album_api_uri}")

        # --- Process HEIC Flag ---
        # Read final value from config (might have been added by check_config_and_authenticate)
        process_heic_config = smugmug.config.get('process_heic', False)
        process_heic_enabled = args.process_heic or process_heic_config  # Command line overrides config
        if process_heic_enabled:
            logging.warning("--- HEIC Processing Enabled ---")
            logging.warning("SmugMug converts HEIC files (including Live Photo video) into static JPGs.")
            logging.warning("The 'live' photo aspect will be lost on SmugMug.")
            logging.warning("Duplicate checking for HEIC files is DISABLED.")
            print(
                "\nWARNING: Processing HEIC files. SmugMug converts them to JPGs, losing the 'live' aspect. Duplicate checking for HEIC is disabled.")

        # --- Get Media Items from Google Photos ---
        logging.info("Fetching media items from Google Photos...")
        photos = google_photos.get_photos(args.google_photos_album_id)
        total_items = len(photos)
        if total_items == 0:
            logging.info("No media items found in the specified Google Photos location. Nothing to transfer.")
            sys.exit(0)  # Successful exit, nothing to do

        logging.info(f"Found {total_items} items in Google Photos.")
        processed_count = 0
        skipped_count = 0
        uploaded_count = 0
        duplicate_count = 0
        error_count = 0

        logging.info(
            f"Starting transfer process... (Dry Run: {args.dry_run}, Ignore Photos: {args.ignore_photos}, Ignore Videos: {args.ignore_videos})")

        # --- Main Processing Loop ---
        for item in photos:
            processed_count += 1
            filename = item.get('filename')
            media_item_id = item['id']
            mime_type = item.get('mimeType', '')
            product_url = item.get('productUrl', '#')  # URL to view on Google Photos

            if not filename or not mime_type:
                logging.warning(f"Skipping item {media_item_id} ({product_url}) due to missing filename or mimeType.")
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
                    f"Skipping {processed_count}/{total_items}: HEIC file '{filename}' ({media_item_id}) as processing is disabled.")
                skipped_count += 1
                continue
            if args.ignore_photos and not is_video:
                logging.info(
                    f"Skipping {processed_count}/{total_items}: Photo '{filename}' ({media_item_id}) due to --ignore-photos flag.")
                skipped_count += 1
                continue
            if args.ignore_videos and is_video:
                logging.info(
                    f"Skipping {processed_count}/{total_items}: Video '{filename}' ({media_item_id}) due to --ignore-videos flag.")
                skipped_count += 1
                continue

            logging.info(
                f"Processing {processed_count}/{total_items} - {item_type}: '{filename}' ({media_item_id}){' (HEIC)' if is_heic else ''}")
            logging.debug(f"  MimeType: {mime_type}, Google URL: {product_url}")

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
                if is_video:
                    # Check videos by filename only
                    exists_on_smugmug = smugmug.check_media_exists(target_album_key, filename, mime_type)
                    if exists_on_smugmug:
                        log_reason = "filename match"
                        logging.info(f"  FOUND on SmugMug ({log_reason}).")
                        duplicate_count += 1
                    else:
                        logging.info("  Not found on SmugMug by filename. Will proceed.")
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
                        # Clean up the downloaded temp file
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
                    if exists_on_smugmug:
                        log_reason = f"matching MD5 hash: {google_file_hash}"
                        logging.info(f"  FOUND on SmugMug ({log_reason}).")
                        duplicate_count += 1
                        # Clean up downloaded file *now* if it already exists
                        if os.path.exists(temp_file_path):
                            try:
                                os.remove(temp_file_path)
                                logging.debug(f"  Removed temp file {temp_file_path} for duplicate item.")
                                temp_file_path = None  # Clear path variable
                            except Exception as e:
                                logging.warning(
                                    f"  Could not remove temp file {temp_file_path} after duplicate check: {e}")
                    else:
                        logging.info(f"  Not found on SmugMug by hash ({google_file_hash}). Will proceed.")
                        # Keep temp_file_path as we need it for upload

            # --- Process Based on Existence ---
            if exists_on_smugmug:
                # Handle optional deletion from Google Photos (currently simulated)
                if args.delete_from_google:
                    if args.dry_run:
                        logging.info(
                            f"  [DRY RUN] Would simulate removal of '{filename}' ({media_item_id}) from Google Photos (as it exists on SmugMug).")
                        # google_photos.remove_photo(media_item_id, dry_run=True) # Simulate
                    else:
                        # Actual deletion is not supported by API
                        logging.warning(
                            f"  Skipping deletion of '{filename}' ({media_item_id}) from Google Photos - API does not support.")
                        # If API supported it, would prompt here:
                        # print(f"Media '{filename}' already exists on SmugMug. Confirm deletion from Google Photos? (yes/no): ", end="")
                        # confirmation = input().lower()
                        # if confirmation == 'yes': ... google_photos.remove_photo ...
                # Continue to next item in the loop
                continue

            # --- Item Does Not Exist on SmugMug (or is HEIC) ---
            # Need to download if not already downloaded (videos, HEIC, or images if download failed earlier somehow)
            if not temp_file_path or not os.path.exists(temp_file_path):
                # This condition covers videos (not downloaded yet) and HEIC files
                # It also covers images if the initial download for hashing failed but we decided to proceed (unlikely)
                logging.debug(f"  Downloading {'video' if is_video else 'HEIC/image'} '{filename}' for upload...")
                temp_file_path, _, _, _, downloaded_mime_type = google_photos.download_photo(item)

                if not temp_file_path:
                    logging.error(f"  Skipping '{filename}' due to download error during upload phase.")
                    skipped_count += 1
                    error_count += 1
                    continue  # Skip to next item
                else:
                    # Use the mime type reported by the download if available, otherwise stick to original
                    if downloaded_mime_type: mime_type = downloaded_mime_type

            # --- Upload to SmugMug ---
            if args.dry_run:
                logging.info(
                    f"  [DRY RUN] Would upload '{filename}' ({item_type}) from {temp_file_path} to SmugMug album URI: {target_album_api_uri}")
                # In dry run, clean up the temp file now
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        logging.debug(f"  [DRY RUN] Removed temporary file: {temp_file_path}")
                    except Exception as e:
                        logging.warning(f"  [DRY RUN] Could not remove temporary file {temp_file_path}: {e}")
            else:
                # Perform the actual upload
                logging.info(f"  Uploading '{filename}' ({item_type}) to SmugMug...")
                if smugmug.upload_media(target_album_api_uri, temp_file_path, filename, mime_type):
                    logging.info(f"  Successfully initiated transfer of '{filename}' to SmugMug.")
                    uploaded_count += 1
                    # Deletion after successful upload (simulated)
                    if args.delete_from_google:
                        logging.warning(
                            f"  Skipping deletion of '{filename}' ({media_item_id}) from Google Photos after upload - API does not support.")
                        # If API supported it:
                        # print(f"Media '{filename}' uploaded. Confirm deletion from Google Photos? (yes/no): ", end="") etc.
                else:
                    logging.error(f"  Failed to transfer '{filename}' to SmugMug.")
                    error_count += 1
                    # Temp file is cleaned up inside upload_media's finally block

        # --- End of Loop ---
        logging.info("-" * 50)
        logging.info("Transfer Process Summary:")
        logging.info(f"  Total items retrieved from Google Photos: {total_items}")
        logging.info(f"  Items processed: {processed_count}")
        logging.info(f"  Successfully uploaded: {uploaded_count}")
        logging.info(f"  Found as duplicates on SmugMug: {duplicate_count}")
        logging.info(f"  Skipped by ignore flags/HEIC setting: {skipped_count}")
        logging.info(f"  Errors encountered (download/upload/hash): {error_count}")
        logging.info(f"  Dry Run mode: {args.dry_run}")
        logging.info(
            f"  Deletion from Google Photos requested: {args.delete_from_google} (Simulated - API Unsupported)")
        logging.info("-" * 50)

    except KeyboardInterrupt:
        logging.warning("Keyboard interrupt detected. Shutting down gracefully...")
        print("\nKeyboard interrupt received. Cleaning up...")
        # Allow finally block to run
    except Exception as e:
        # Catch any unexpected errors in the main block
        logging.critical(f"An critical unexpected error occurred in the main processing loop: {e}", exc_info=True)
        print(f"\nAn critical unexpected error occurred: {e}")
    finally:
        # Ensure cleanup is always called, passing the google_photos object
        cleanup(google_photos)


if __name__ == "__main__":
    main()
