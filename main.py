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
import hashlib

# Third-party imports
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from requests_oauthlib import OAuth1Session

# Local module imports
from smugmug_module import SmugMug

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration Files ---
GOOGLE_PHOTOS_CREDENTIALS_FILE = 'google_photos_credentials.json'  # Path to your Google Photos API credentials file
SMUGMUG_CONFIG_FILE = 'smugmug_config.json'  # Path to your SmugMug API configuration file
GOOGLE_PHOTOS_TOKEN_FILE = 'google_photos_token.json'  # File to store Google Photos access token
BATCH_SIZE = 50  # Number of photos to process in each batch

# Google Photos API Scopes (removed edit scope)
GOOGLE_PHOTOS_SCOPES = ['https://www.googleapis.com/auth/photoslibrary.readonly',
                        'https://www.googleapis.com/auth/photoslibrary.appendonly']


def load_smugmug_config():
    """Loads SmugMug API configuration from a JSON file."""
    try:
        with open(SMUGMUG_CONFIG_FILE, 'r') as f:
            config = json.load(f)
            return config
    except FileNotFoundError:
        logging.error(f"SmugMug configuration file not found: {SMUGMUG_CONFIG_FILE}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON from SmugMug configuration file: {SMUGMUG_CONFIG_FILE}")
        return None


def authenticate_google_photos():
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists(GOOGLE_PHOTOS_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_PHOTOS_TOKEN_FILE, GOOGLE_PHOTOS_SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_PHOTOS_CREDENTIALS_FILE, GOOGLE_PHOTOS_SCOPES)
                creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                logging.error(f"Google Photos credentials file not found: {GOOGLE_PHOTOS_CREDENTIALS_FILE}. Please ensure it exists.")
                return None
        # Save the credentials for the next run
        try:
            with open(GOOGLE_PHOTOS_TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        except IOError as e:
            logging.error(f"Error saving Google Photos token to {GOOGLE_PHOTOS_TOKEN_FILE}: {e}")


    service = None
    try:
        service = build('photoslibrary', 'v1', credentials=creds,
                        discoveryServiceUrl='https://photoslibrary.googleapis.com/$discovery/rest?version=v1')
        logging.info("Successfully authenticated with Google Photos API.")
        # Optional: Verify credentials by making a small API call
        service.mediaItems().list(pageSize=1).execute()
        logging.debug("Google Photos API credentials verified.")
    except HttpError as error:
        logging.error(f'An HTTP error occurred during Google Photos API authentication or verification: {error}')
        service = None # Ensure service is None if verification fails
    except Exception as e:
        logging.error(f'An unexpected error occurred during Google Photos API authentication: {e}')
        service = None

    return service


def get_google_photos(service, album_id=None):
    """Retrieves a list of photos from the Google Photos library, optionally from a specific album."""
    photos = []
    nextPageToken = None
    # Construct the body of the request. This is required for both library and album searches.
    body = {
        'pageSize': BATCH_SIZE,
    }
    if album_id:
        body['albumId'] = album_id

    # Determine which method to use based on album_id
    method = service.mediaItems().search if album_id else service.mediaItems().list

    while True:
        try:
            if album_id:
                 logging.info(f"Fetching photos from Google Photos album ID: {album_id}")
                 # Search method requires the body payload
                 results = method(body=body).execute() # Pass body directly to search
                 items = results.get('mediaItems')
            else:
                logging.info("Fetching all photos from Google Photos library.")
                # List method takes parameters directly
                results = method(pageSize=BATCH_SIZE, pageToken=nextPageToken).execute()
                items = results.get('mediaItems')

            if not items:
                if nextPageToken: # If there was a token but no items, something might be wrong or it's the end
                     logging.info("No more items found with the current page token.")
                else: # No token and no items means empty
                     logging.info("No photos found in Google Photos.")
                break

            photos.extend(items)
            # Pagination: next page token might be in the results for list *or* search
            nextPageToken = results.get('nextPageToken')
            if nextPageToken:
                 # Update the body for the next page of search results
                 if album_id:
                      body['pageToken'] = nextPageToken
            else:
                break # No more pages

        except HttpError as error:
            logging.error(f'An error occurred while retrieving photos from Google Photos: {error}')
            break
        except Exception as e:
             logging.error(f'An unexpected error occurred while retrieving photos from Google Photos: {e}')
             break # Exit loop on unexpected error as well

    logging.info(f"Retrieved {len(photos)} photos from Google Photos.")
    return photos


def download_google_photo(service, media_item):
    """Downloads a media item (photo or video) from Google Photos in its original quality."""
    media_item_id = media_item['id']
    filename = media_item.get('filename', f"media_{media_item_id}.dat") # Add a default extension
    mime_type = media_item.get('mimeType', '')
    is_video = mime_type.startswith('video/')

    try:
        # Use the baseUrl directly from the media item list response if available,
        # or fetch full details if needed (though baseUrl is usually present).
        # Fetching details again is safer if the initial list response is minimal.
        item_details = service.mediaItems().get(mediaItemId=media_item_id).execute()
        base_url = item_details.get('baseUrl')
        if not base_url:
            logging.error(f"Could not get baseUrl for media item {media_item_id} ({filename}).")
            return None, None, None, None, None # Include mime_type in return

        # Append appropriate parameter for download based on type.
        # =dv for videos (original quality), =d for photos (original quality).
        download_param = '=dv' if is_video else '=d'
        download_url = base_url + download_param

        logging.debug(f"Attempting download from URL: {download_url}")
        media_response = requests.get(download_url, stream=True)  # Use stream=True for potentially large files
        media_response.raise_for_status()  # Raise an exception for bad status codes

        # Construct temporary file path
        temp_dir = "temp_downloads"
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        # Ensure filename is safe for filesystem and maybe append a unique ID to avoid conflicts
        safe_filename = "".join([c for c in filename if c.isalnum() or c in ('.', '_', '-')])
        file_path = os.path.join(temp_dir, f"temp_{media_item_id}_{safe_filename}")

        # Write content to file
        downloaded_size = 0
        with open(file_path, 'wb') as f:
            for chunk in media_response.iter_content(chunk_size=8192):  # Write in chunks (8KB)
                f.write(chunk)
                downloaded_size += len(chunk)

        logging.info(f"Downloaded {filename} ({media_item_id}, {'video' if is_video else 'photo'}) to {file_path} ({downloaded_size} bytes).")

        # Get metadata (width/height might not apply to video, but harmless to get if available)
        metadata = item_details.get('mediaMetadata', {})
        width = metadata.get('width')
        height = metadata.get('height')
        return file_path, filename, width, height, mime_type # Return mime_type

    except HttpError as error:
        logging.error(
            f'An API error occurred while downloading {filename} ({media_item_id}) from Google Photos: {error}')
        return None, None, None, None, None
    except requests.exceptions.RequestException as e:
        logging.error(
            f'A network error occurred during the download request for {filename} ({media_item_id}): {e}')
        if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
             logging.error(f"HTTP Status Code: {e.response.status_code}")
        if hasattr(e, 'response') and hasattr(e.response, 'text'):
             logging.error(f"Response Text: {e.response.text}")
        return None, None, None, None, None
    except IOError as e:
        logging.error(f"An error occurred writing temporary file {file_path}: {e}")
        return None, None, None, None, None
    except Exception as e:
         logging.error(f"An unexpected error occurred during download of '{filename}' ({media_item_id}): {e}")
         return None, None, None, None, None


def remove_from_google_photos(service, media_item_id, dry_run=False):
    """Removes a photo from Google Photos."""
    if dry_run:
        logging.info(f"[DRY RUN] Would remove photo from Google Photos: {media_item_id}")
        return True # Simulate success in dry run
    try:
        # The batchRemove endpoint expects a list of IDs
        response = service.mediaItems().batchRemove(mediaItemIds=[media_item_id]).execute()
        # batchRemove success response is usually an empty body {} or just status 204 No Content
        # The presence of a response body with errors would indicate failure for specific items.
        # A successful response with no errors means the request was accepted,
        # but individual item failures might be in the response if not empty.
        # Let's check for a non-empty response which might indicate errors.
        if response: # If response is not empty, it might contain error details
             logging.warning(f"Google Photos batchRemove response was not empty for {media_item_id}: {response}. Check response for errors.")
             # Depending on the specific error structure, you might need more detailed parsing here.
             # For now, assume non-empty indicates a potential issue or partial failure.
             return False # Indicate failure if response is not empty (suggests errors)
        else: # Empty response or 204 implies success for the requested item(s)
            logging.info(f"Successfully removed photo from Google Photos: {media_item_id}")
            return True
    except HttpError as error:
        logging.error(f'An API error occurred while removing photo {media_item_id} from Google Photos: {error}')
        # Log response text if available for more details
        if hasattr(error, 'resp') and hasattr(error.resp, 'text'):
             logging.error(f"Google Photos API Response Text: {error.resp.text}")
        return False
    except Exception as e:
         logging.error(f"An unexpected error occurred during removal of photo {media_item_id} from Google Photos: {e}")
         return False


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

    # Authenticate Google Photos API
    google_photos_service = authenticate_google_photos()
    if not google_photos_service:
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
    google_photos = get_google_photos(google_photos_service, args.google_photos_album_id)
    total_items = len(google_photos)
    processed_count = 0
    skipped_count = 0

    logging.info(f"Starting transfer process (Dry Run: {args.dry_run}, Ignore Photos: {args.ignore_photos}, Ignore Videos: {args.ignore_videos}).")

    for item in google_photos:
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
            # download_google_photo returns file_path, filename, width, height, mime_type
            temp_file_path, _, _, _, downloaded_mime_type = download_google_photo(google_photos_service, item)

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
                else:
                    print(f"Media '{filename}' already exists on SmugMug. Confirm deletion from Google Photos? (yes/no): ", end="")
                    confirmation = input().lower()
                    if confirmation == 'yes':
                        if remove_from_google_photos(google_photos_service, media_item_id):
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
                # download_google_photo returns file_path, filename, width, height, mime_type
                temp_file_path, _, _, _, downloaded_mime_type = download_google_photo(google_photos_service, item)

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
                            if remove_from_google_photos(google_photos_service, media_item_id):
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
