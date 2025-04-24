# Google Photos to SmugMug Transfer Script
#
# This script facilitates transferring media from Google Photos to SmugMug.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#
__version__ = "1.1" # Updated version number

# Standard library imports
import argparse
import json
import logging
import os

# Third-party imports
import requests

# Local module imports
from smugmug_module import SmugMug
from google_photos_module import GooglePhotos

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration Files ---
GOOGLE_PHOTOS_CREDENTIALS_FILE = 'google_photos_credentials.json'  # Path to your Google Photos API credentials file
SMUGMUG_CONFIG_FILE = 'smugmug_config.json'  # Path to your SmugMug API configuration file
GOOGLE_PHOTOS_TOKEN_FILE = 'google_photos_token.json'  # File to store Google Photos access token
BATCH_SIZE = 50  # Number of photos to process in each batch



def main():
    """Main function to orchestrate the photo transfer."""
    parser = argparse.ArgumentParser(description="Transfer photos from Google Photos to SmugMug.")
    parser.add_argument('--delete-from-google', action='store_true',
                        help='Delete photos from Google Photos after successful transfer to SmugMug (requires confirmation).')
    parser.add_argument('--google-photos-album-id', type=str,
                        help='Process photos only from the specified Google Photos album ID.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Perform a dry run: download and check existence, but do not upload to SmugMug or delete from Google Photos.')
    parser.add_argument('--ignore-photos', action='store_true',
                        help='Skip processing media items identified as photos (images).')
    parser.add_argument('--ignore-videos', action='store_true',
                        help='Skip processing media items identified as videos.')

    args = parser.parse_args()

    # Initialize SmugMug with configuration file
    smugmug = SmugMug(SMUGMUG_CONFIG_FILE)
    smugmug_config = smugmug.config
    if not smugmug_config:
        logging.error("Failed to load SmugMug configuration. Exiting.")
        return

    # Check if API key and secret are in the config file
    api_key = smugmug_config.get('api_key')
    api_secret = smugmug_config.get('api_secret')

    if not api_key or not api_secret:
        print("SmugMug API key and secret not found in smugmug_config.json.")
        print("Please configure your SmugMug API key and secret in smugmug_config.json.")
        return

    # Initialize Google Photos with configuration files
    google_photos = GooglePhotos(GOOGLE_PHOTOS_CREDENTIALS_FILE, GOOGLE_PHOTOS_TOKEN_FILE, BATCH_SIZE)
    if not google_photos.is_authenticated():
        logging.error("Failed to authenticate with Google Photos. Exiting.")
        return

    # Authenticate with SmugMug
    if not smugmug.is_authenticated():
        if not smugmug.authenticate():
            logging.error("Failed to authenticate with SmugMug. Exiting.")
            return

    # Get album details now that config is confirmed loaded
    album_key = smugmug_config.get('album_key')
    album_api_uri = smugmug_config.get('album_api_uri')
    if not album_key or not album_api_uri:
        logging.error("SmugMug 'album_key' and 'album_api_uri' must be set in the configuration file (smugmug_config.json). Exiting.")
        print("Please ensure 'album_key' (e.g., 'ABCDE') and 'album_api_uri' (e.g., '/api/v2/album/ABCDE') are configured in smugmug_config.json.")
        return

    # Get media items from Google Photos
    photos = google_photos.get_photos(args.google_photos_album_id)
    total_items = len(photos)
    processed_count = 0
    skipped_count = 0

    logging.info(f"Starting transfer process (Dry Run: {args.dry_run}, Ignore Photos: {args.ignore_photos}, Ignore Videos: {args.ignore_videos}).")

    for item in photos:
        processed_count += 1
        filename = item.get('filename')
        media_item_id = item['id']
        mime_type = item.get('mimeType', '')

        if not filename or not mime_type:
            logging.warning(f"Skipping item {media_item_id} due to missing filename or mimeType.")
            skipped_count += 1
            continue

        is_video = mime_type.startswith('video/')
        item_type = "Video" if is_video else "Photo"

        # --- Apply Ignore Flags ---
        if args.ignore_photos and not is_video:
            logging.info(f"Skipping photo '{filename}' ({media_item_id}) due to --ignore-photos flag.")
            skipped_count += 1
            continue
        if args.ignore_videos and is_video:
            logging.info(f"Skipping video '{filename}' ({media_item_id}) due to --ignore-videos flag.")
            skipped_count += 1
            continue

        logging.info(f"Processing {processed_count}/{total_items} - {item_type}: '{filename}' ({media_item_id})")

        # --- Existence Check ---
        exists_on_smugmug = False
        temp_file_path = None
        file_hash = None # Will store MD5 for images

        if is_video:
            # For videos, check existence by filename *before* downloading
            logging.debug(f"Checking for video '{filename}' on SmugMug by filename...")
            exists_on_smugmug = smugmug.check_media_exists(album_key, filename, mime_type)
        else:
            # For images, download first to calculate MD5 hash for existence check
            logging.debug(f"Downloading image '{filename}' to calculate MD5 hash...")
            # download_photo returns file_path, filename, width, height, mime_type
            temp_file_path, _, _, _, downloaded_mime_type = google_photos.download_photo(item)

            if not temp_file_path:
                logging.error(f"Skipping image '{filename}' due to download error.")
                # Clean up temp file if it was partially created/exists
                if temp_file_path and os.path.exists(temp_file_path):
                    try: os.remove(temp_file_path)
                    except Exception as e: logging.warning(f"Could not remove partial temp file {temp_file_path}: {e}")
                skipped_count += 1
                continue

            # Calculate MD5 for the downloaded image file
            file_hash = smugmug.calculate_file_hash(temp_file_path, hash_algorithm='md5')
            if not file_hash:
                logging.error(f"Skipping image '{filename}' due to MD5 hash calculation error.")
                # Clean up the downloaded temp file
                if os.path.exists(temp_file_path):
                    try: os.remove(temp_file_path)
                    except Exception as e: logging.warning(f"Could not remove temp file {temp_file_path} after hash error: {e}")
                skipped_count += 1
                continue
            logging.debug(f"Calculated MD5 for '{filename}': {file_hash}")
            # Check SmugMug for image existence by MD5 hash
            exists_on_smugmug = smugmug.check_media_exists(album_key, filename, mime_type, file_hash=file_hash)

        # --- Process Based on Existence ---
        if exists_on_smugmug:
            log_reason = "filename match" if is_video else f"matching MD5 hash: {file_hash}"
            logging.info(f"Media '{filename}' already exists on SmugMug ({log_reason}).")
            # Clean up downloaded file if it exists (only images were downloaded at this stage for check)
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logging.debug(f"Removed temporary file: {temp_file_path}")
                except Exception as e:
                     logging.warning(f"Could not remove temporary file {temp_file_path} after existence check: {e}")

            # Handle optional deletion from Google Photos if not in dry run
            if args.delete_from_google:
                if args.dry_run:
                    logging.info(f"[DRY RUN] Would ask to confirm deletion of '{filename}' ({media_item_id}) from Google Photos.")
                    logging.info(f"[DRY RUN] Would remove '{filename}' ({media_item_id}) from Google Photos if confirmed.")
                    google_photos.remove_photo(media_item_id, dry_run=True)
                else:
                    print(f"Media '{filename}' already exists on SmugMug. Confirm deletion from Google Photos? (yes/no): ", end="")
                    confirmation = input().lower()
                    if confirmation == 'yes':
                        if google_photos.remove_photo(media_item_id):
                            logging.info(f"Removed '{filename}' ({media_item_id}) from Google Photos.")
                        else:
                            logging.warning(f"Failed to remove '{filename}' ({media_item_id}) from Google Photos.")
                    else:
                        logging.info(f"Skipping deletion of '{filename}' ({media_item_id}) from Google Photos.")
            else:
                logging.debug(
                    f"Skipping deletion check for '{filename}' ({media_item_id}) from Google Photos (--delete-from-google not set).")

        else: # Item does NOT exist on SmugMug
            log_reason = "filename" if is_video else f"MD5 hash: {file_hash}"
            logging.info(f"Media '{filename}' ({log_reason}) not found on SmugMug. Proceeding with upload.")

            # Download video if it wasn't already downloaded for hash check (images were)
            if is_video:
                logging.debug(f"Downloading video '{filename}' for upload...")
                # download_photo returns file_path, filename, width, height, mime_type
                temp_file_path, _, _, _, downloaded_mime_type = google_photos.download_photo(item)

                if not temp_file_path:
                    logging.error(f"Skipping video '{filename}' due to download error during upload phase.")
                    # Clean up temp file if it was partially created/exists
                    if temp_file_path and os.path.exists(temp_file_path):
                         try: os.remove(temp_file_path)
                         except Exception as e: logging.warning(f"Could not remove partial temp file {temp_file_path}: {e}")
                    skipped_count += 1
                    continue

            # Ensure we have a temp_file_path before attempting upload (downloaded for images earlier, or for videos now)
            if not temp_file_path or not os.path.exists(temp_file_path):
                logging.error(f"Cannot upload '{filename}', temporary file path is missing or invalid: {temp_file_path}")
                skipped_count += 1
                continue

            # --- Upload to SmugMug if not dry run ---
            if args.dry_run:
                logging.info(f"[DRY RUN] Would upload '{filename}' ({item_type}) from {temp_file_path} to SmugMug album URI: {album_api_uri}")
                # In dry run, manually clean up the temp file since we're not calling smugmug.upload_media
                if os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                        logging.debug(f"[DRY RUN] Removed temporary file: {temp_file_path}")
                    except Exception as e:
                         logging.warning(f"[DRY RUN] Could not remove temporary file {temp_file_path} after dry run processing: {e}")
            else:
                # Upload the downloaded file (image or video)
                logging.info(f"Uploading '{filename}' ({item_type}) to SmugMug...")
                if smugmug.upload_media(album_api_uri, temp_file_path, filename, mime_type):
                    logging.info(f"Successfully initiated transfer of '{filename}' ({item_type}) to SmugMug.")

                    # Handle optional deletion from Google Photos after successful upload
                    if args.delete_from_google:
                        print(f"Media '{filename}' was uploaded to SmugMug. Confirm deletion from Google Photos? (yes/no): ", end="")
                        confirmation = input().lower()
                        if confirmation == 'yes':
                            if google_photos.remove_photo(media_item_id):
                                logging.info(f"Removed '{filename}' ({media_item_id}) from Google Photos after successful upload.")
                            else:
                                logging.warning(f"Failed to remove '{filename}' ({media_item_id}) from Google Photos after successful upload.")
                        else:
                            logging.info(f"Skipping deletion of '{filename}' ({media_item_id}) from Google Photos after successful upload.")
                else:
                    logging.error(f"Failed to transfer '{filename}' ({item_type}) to SmugMug.")
                    skipped_count += 1 # Count failed uploads as skipped for reporting

    logging.info("-" * 30)
    logging.info("Transfer Process Summary:")
    logging.info(f"Total items retrieved from Google Photos: {total_items}")
    logging.info(f"Items processed (including skipped by flags): {processed_count}")
    logging.info(f"Items skipped by --ignore flags or errors: {skipped_count}")
    logging.info(f"Dry Run mode: {args.dry_run}")
    logging.info(f"Deletion from Google Photos enabled: {args.delete_from_google}")
    logging.info("-" * 30)

    # Final cleanup of the temporary download directory if it exists and is empty
    temp_dir = "temp_downloads"
    if os.path.exists(temp_dir):
        try:
            if not os.listdir(temp_dir): # Check if directory is empty
                os.rmdir(temp_dir)
                logging.info(f"Removed empty temporary download directory: {temp_dir}")
        except OSError as e:
            logging.warning(f"Could not remove temporary download directory {temp_dir}: {e}")


if __name__ == "__main__":
    main()

