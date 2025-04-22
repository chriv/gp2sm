# Google Photos to SmugMug Transfer Script
#
# This script facilitates transferring media from Google Photos to SmugMug.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#
__version__ = "1.0"

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


def save_smugmug_config(config):
    """Saves SmugMug API configuration to a JSON file."""
    try:
        with open(SMUGMUG_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
            logging.info(f"SmugMug configuration saved to {SMUGMUG_CONFIG_FILE}")
    except IOError as e:
        logging.error(f"Error saving SmugMug configuration to {SMUGMUG_CONFIG_FILE}: {e}")


def authenticate_google_photos():
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('google_photos_token.json'):
        creds = Credentials.from_authorized_user_file('google_photos_token.json', GOOGLE_PHOTOS_SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'google_photos_credentials.json', GOOGLE_PHOTOS_SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('google_photos_token.json', 'w') as token:
            token.write(creds.to_json())

    service = None
    try:
        service = build('photoslibrary', 'v1', credentials=creds,
                        discoveryServiceUrl='https://photoslibrary.googleapis.com/$discovery/rest?version=v1')
        logging.info("Successfully authenticated with Google Photos API.")
    except HttpError as error:
        logging.error(f'An error occurred during Google Photos API authentication: {error}')

    # Test building Drive API service (moved before return)
    try:
        drive_service = build('drive', 'v3', credentials=creds)
        print("Successfully built Drive API service.")
    except Exception as e:
        print(f"Error building Drive API service: {e}")

    return service


def obtain_smugmug_oauth_tokens(smugmug_api_key, smugmug_api_secret):
    """Obtains SmugMug OAuth tokens using the OAuth 1.0a flow."""
    request_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getRequestToken'
    authorize_url = 'https://secure.smugmug.com/services/oauth/1.0a/authorize'
    access_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getAccessToken'

    smugmug = OAuth1Session(smugmug_api_key, client_secret=smugmug_api_secret, callback_uri='oob')
    logging.info("Fetching request token from SmugMug...")
    try:
        fetch_response = smugmug.fetch_request_token(request_token_url)
        if fetch_response:
            url = smugmug.authorization_url(authorize_url)
            print(f"Please open the following URL in your browser and authorize the application:\n{url}")
            verifier = input("Enter the verifier code you received after authorizing: ")
            token_response = smugmug.fetch_access_token(access_token_url, verifier=verifier)
            if token_response:
                return smugmug.token.get('oauth_token'), smugmug.token.get('oauth_token_secret')
            else:
                logging.error("Failed to fetch access token from SmugMug.")
                return None, None
        else:
            logging.error("Failed to fetch request token from SmugMug.")
            return None, None
    except Exception as e:
        logging.error(f"An error occurred during SmugMug OAuth: {e}")
        return None, None


def get_smugmug_auth(smugmug_config):
    """
    Returns an OAuth1 object for SmugMug API authentication and the potentially updated config,
    obtaining tokens if needed.
    Accepts the loaded smugmug_config dictionary.
    """
    if not smugmug_config:
        logging.error("SmugMug configuration not provided to get_smugmug_auth.")
        return None, None  # Return None for auth and config

    api_key = smugmug_config.get('api_key')
    api_secret = smugmug_config.get('api_secret')
    oauth_token = smugmug_config.get('oauth_token')
    oauth_token_secret = smugmug_config.get('oauth_token_secret')

    if not api_key or not api_secret:
        logging.error("SmugMug API Key and Secret must be configured.")
        return None, smugmug_config  # Return None for auth, but original config

    if not oauth_token or not oauth_token_secret or oauth_token == "YOUR_SMUGMUG_OAUTH_TOKEN":
        logging.info("SmugMug OAuth tokens not found or invalid in config. Initiating authorization flow...")
        new_oauth_token, new_oauth_token_secret = obtain_smugmug_oauth_tokens(api_key, api_secret)
        if new_oauth_token and new_oauth_token_secret:
            smugmug_config['oauth_token'] = new_oauth_token
            smugmug_config['oauth_token_secret'] = new_oauth_token_secret
            save_smugmug_config(smugmug_config)
            oauth_token = new_oauth_token
            oauth_token_secret = new_oauth_token_secret
        else:
            logging.error("Failed to obtain SmugMug OAuth tokens. Please check the logs and try again.")
            # Return None for auth, but the config might have been partially updated (though unlikely useful)
            return None, smugmug_config

    auth = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token,
                         resource_owner_secret=oauth_token_secret)
    logging.info("SmugMug API authentication configured.")
    return auth, smugmug_config  # Return auth session and potentially updated config


def get_google_photos(service, album_id=None):
    """Retrieves a list of photos from the Google Photos library, optionally from a specific album."""
    photos = []
    nextPageToken = None
    while True:
        try:
            if album_id:
                results = service.albums().get(albumId=album_id).execute()
                album_title = results.get('title', 'Specified Album')
                logging.info(f"Fetching photos from Google Photos album: '{album_title}' (ID: {album_id})")
                media_results = service.mediaItems().search(
                    albumId=album_id,
                    pageSize=BATCH_SIZE,
                    pageToken=nextPageToken
                ).execute()
                items = media_results.get('mediaItems')
            else:
                logging.info("Fetching all photos from Google Photos library.")
                results = service.mediaItems().list(pageSize=BATCH_SIZE, pageToken=nextPageToken).execute()
                items = results.get('mediaItems')

            if not items:
                logging.info("No more photos found in Google Photos.")
                break
            photos.extend(items)
            nextPageToken = results.get('nextPageToken')
            if not nextPageToken:
                break
        except HttpError as error:
            logging.error(f'An error occurred while retrieving photos from Google Photos: {error}')
            break
    logging.info(f"Retrieved {len(photos)} photos from Google Photos.")
    return photos


def calculate_file_hash(file_path):
    """Calculates the SHA256 hash of a file."""
    # hashlib is imported globally now
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as afile:
            buf = afile.read(65536)
            while len(buf) > 0:
                hasher.update(buf)
                buf = afile.read(65536)
            return hasher.hexdigest()
    except FileNotFoundError:
        logging.error(f"File not found: {file_path}")
        return None
    except Exception as e:
        logging.error(f"Error calculating file hash for {file_path}: {e}")
        return None


def check_photo_exists_smugmug(auth, album_key, filename, mime_type, file_hash=None):
    """
    Checks if a media item exists in the specified SmugMug album.
    - For images (mime_type starting with 'image/'), checks by MD5 hash.
    - For videos (mime_type starting with 'video/'), checks by filename.
    Accepts the authenticated session, album_key, filename, mime_type, and optional MD5 file_hash (required for images).
    Returns True if found, False otherwise.
    """
    if not album_key:
        logging.warning("No SmugMug Album Key provided. Cannot check for media existence.")
        return False  # Cannot check without album key

    is_video = mime_type.startswith('video/')
    if not is_video and not file_hash:
        logging.warning(f"Image file '{filename}' requires a file hash for existence check, but none was provided.")
        return False

    album_uri = f"/api/v2/album/{album_key}"
    album_url = f'https://api.smugmug.com{album_uri}!images'  # Get the images endpoint
    next_page_url = album_url
    headers = {'Accept': 'application/json'}

    while next_page_url:
        try:
            response = auth.get(next_page_url, headers=headers)
            response.raise_for_status()
            data = response.json()

            if 'Response' in data and 'AlbumImage' in data['Response']:
                for item in data['Response']['AlbumImage']:  # Renamed 'image' to 'item' for clarity
                    item_filename = item.get('FileName')

                    if is_video:
                        # Check by filename for videos
                        if item_filename and filename == item_filename:
                            logging.info(f"Video '{filename}' found on SmugMug by filename match.")
                            return True
                    else:
                        # Check by hash for images
                        smugmug_hash = item.get('ArchivedMD5')
                        # Ensure file_hash is not None before comparing
                        if file_hash and smugmug_hash and file_hash.lower() == smugmug_hash.lower():
                            logging.info(f"Image '{filename}' found on SmugMug with matching MD5 hash: {file_hash}")
                            return True  # Exact match found

            # Pagination logic
            pages_info = data.get('Response', {}).get('Pages')
            if pages_info and 'NextPage' in pages_info:
                next_page_url = f"https://api.smugmug.com{pages_info['NextPage']}"  # Ensure full URL
            else:
                next_page_url = None

        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking for photo existence on SmugMug: {e}")
            return False

    return False


def download_google_photo(service, media_item):
    """Downloads a media item (photo or video) from Google Photos in its original quality."""
    media_item_id = media_item['id']
    filename = media_item.get('filename', f"media_{media_item_id}")
    mime_type = media_item.get('mimeType', '')
    is_video = mime_type.startswith('video/')

    try:
        # Note: mediaItems().get() might not be needed if baseUrl is already in the list response
        # However, keeping it for now to ensure we have the latest baseUrl
        item_details = service.mediaItems().get(mediaItemId=media_item_id).execute()
        base_url = item_details.get('baseUrl')
        if not base_url:
            logging.error(f"Could not get baseUrl for media item {media_item_id}")
            return None, None, None, None

        # Append appropriate parameter for download based on type
        download_param = '=dv' if is_video else '=d'
        download_url = base_url + download_param

        logging.debug(f"Attempting download from URL: {download_url}")
        media_response = requests.get(download_url, stream=True)  # Use stream=True for potentially large files
        media_response.raise_for_status()  # Raise an exception for bad status codes

        # Construct temporary file path
        temp_dir = "temp_downloads"  # Consider using a dedicated temp directory
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        file_path = os.path.join(temp_dir, f"temp_{filename}")

        # Write content to file
        with open(file_path, 'wb') as f:
            for chunk in media_response.iter_content(chunk_size=8192):  # Write in chunks
                f.write(chunk)

        logging.info(f"Downloaded {'video' if is_video else 'photo'} from Google Photos: {filename} to {file_path}")

        # Get metadata (width/height might not apply to video, but harmless)
        metadata = item_details.get('mediaMetadata', {})
        width = metadata.get('width')
        height = metadata.get('height')
        return file_path, filename, width, height

    except HttpError as error:
        logging.error(
            f'An API error occurred while downloading {filename} ({media_item_id}) from Google Photos: {error}')
        return None, None, None, None
    except requests.exceptions.RequestException as e:
        logging.error(
            f'A network error occurred during the download request for {filename} ({media_item_id}): {e}')
        return None, None, None, None
    except IOError as e:
        logging.error(f"An error occurred writing temporary file {file_path}: {e}")
        return None, None, None, None


def calculate_md5(file_path):
    """Calculates the MD5 hash of a file."""
    hasher = hashlib.md5()
    try:
        with open(file_path, 'rb') as afile:
            buf = afile.read(65536)
            while len(buf) > 0:
                hasher.update(buf)
                buf = afile.read(65536)
            return hasher.hexdigest()
    except FileNotFoundError:
        logging.error(f"File not found: {file_path}")
        return None
    except Exception as e:
        logging.error(f"Error calculating MD5 hash for {file_path}: {e}")
        return None


def upload_to_smugmug(auth_session, album_api_uri, file_path, filename, mime_type):
    """
    Upload media file to specified Album API URI, given an authenticated session, file path, filename and mime_type.
    Note: SmugMug upload implementation details (headers, endpoint) inspired by
    https://github.com/SkiTheSlicer/smugmug-api-v2-upload by SkiTheSlicer.
    """
    try:
        with open(file_path, 'rb') as media_file:
            media_data = media_file.read()

        headers = {
            'Accept': 'application/json',  # Use string, requests_oauthlib handles encoding
            'Content-Length': str(len(media_data)),
            'Content-MD5': hashlib.md5(media_data).hexdigest(),
            'Content-Type': mime_type,  # Use the provided mime_type
            'X-Smug-AlbumUri': album_api_uri,
            'X-Smug-FileName': filename,
            'X-Smug-ResponseType': 'JSON',
            'X-Smug-Version': 'v2',
        }

        # Use the authenticated session directly for the POST request
        response = auth_session.post('https://upload.smugmug.com/', headers=headers, data=media_data)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        upload_data = response.json()

        # Check the response structure for success indication
        # SmugMug upload response might vary, adjust based on actual API response
        if upload_data.get('stat') == 'ok' and 'Image' in upload_data:  # Check for 'Image' key for success
            image_info = upload_data.get('Image', {})
            status_url = image_info.get('StatusURL')  # URL to check upload status
            image_url = image_info.get('URL')  # Final URL once processed
            logging.info(f"Successfully initiated upload for '{filename}' to SmugMug. Status URL: {status_url}, Final URL (approx): {image_url}")
            # Note: Upload might still be processing on SmugMug's side.
            # For critical applications, you might poll the StatusURL.
            return True
        else:
            logging.error(f"Failed to upload '{filename}' to SmugMug. Response: {upload_data}")
            return False

    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading {filename} to SmugMug: {e}")
        if hasattr(e.response, 'text'):
            logging.error(f"Response Text: {e.response.text}")
        return False
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


def remove_from_google_photos(service, media_item_id):
    """Removes a photo from Google Photos."""
    try:
        response = service.mediaItems().batchRemove(mediaItemIds=[media_item_id]).execute()
        if not response:
            logging.info(f"Successfully removed photo from Google Photos: {media_item_id}")
            return True
        else:
            logging.warning(f"Failed to remove photo {media_item_id} from Google Photos. Response: {response}")
            return False
    except HttpError as error:
        logging.error(f'An error occurred while removing photo {media_item_id} from Google Photos: {error}')
        return False


def main():
    """Main function to orchestrate the photo transfer."""
    parser = argparse.ArgumentParser(description="Transfer photos from Google Photos to SmugMug.")
    parser.add_argument('--delete-from-google', action='store_true',
                        help='Delete photos from Google Photos if they exist on SmugMug (requires confirmation).')
    parser.add_argument('--google-photos-album-id', type=str,
                        help='Process photos only from the specified Google Photos album ID.')
    args = parser.parse_args()

    # Load SmugMug config *before* authenticating with Google Photos
    smugmug_config = load_smugmug_config()
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

    google_photos_service = authenticate_google_photos()
    if not google_photos_service:
        return

    google_photos_service = authenticate_google_photos()
    if not google_photos_service:
        return

    smugmug_config = load_smugmug_config()
    if not smugmug_config:
        logging.error("Failed to load SmugMug configuration. Exiting.")
        return

    smugmug_auth, smugmug_config = get_smugmug_auth(smugmug_config)  # Pass config, get auth and potentially updated config back
    if not smugmug_auth:
        logging.error("Failed to authenticate with SmugMug. Exiting.")
        return

    # Get album details now that config is confirmed loaded and potentially updated
    album_key = smugmug_config.get('album_key')
    album_api_uri = smugmug_config.get('album_api_uri')
    if not album_key or not album_api_uri:
        logging.error("SmugMug 'album_key' and 'album_api_uri' must be set in the configuration file. Exiting.")
        return

    google_photos = get_google_photos(google_photos_service, args.google_photos_album_id)
    total_photos = len(google_photos)
    processed_count = 0

    for photo in google_photos:
        processed_count += 1
        filename = photo.get('filename')
        media_item_id = photo['id']
        mime_type = photo.get('mimeType')

        if not filename or not mime_type:
            logging.warning(f"Skipping item {media_item_id} due to missing filename or mimeType.")
            continue

        logging.info(f"Processing {processed_count}/{total_photos}: {filename} ({media_item_id}, Type: {mime_type})")

        # Determine if it's a video
        is_video = mime_type.startswith('video/')

        # --- Existence Check ---
        exists_on_smugmug = False
        temp_file_path = None
        file_hash = None

        if is_video:
            # For videos, check by filename *before* downloading
            logging.debug(f"Checking for video '{filename}' on SmugMug by filename...")
            exists_on_smugmug = check_photo_exists_smugmug(smugmug_auth, album_key, filename, mime_type)
        else:
            # For images, download first to get hash
            logging.debug(f"Downloading image '{filename}' to calculate hash...")
            temp_file_path, _, _, _ = download_google_photo(google_photos_service, photo)
            if not temp_file_path:
                logging.error(f"Skipping image '{filename}' due to download error.")
                continue
            file_hash = calculate_md5(temp_file_path)
            if not file_hash:
                logging.error(f"Skipping image '{filename}' due to hash calculation error.")
                if os.path.exists(temp_file_path): os.remove(temp_file_path)
                continue
            logging.debug(f"Calculated MD5 for '{filename}': {file_hash}")
            # Check by hash
            exists_on_smugmug = check_photo_exists_smugmug(smugmug_auth, album_key, filename, mime_type, file_hash=file_hash)

        # --- Process Based on Existence ---
        if exists_on_smugmug:
            log_reason = "filename match" if is_video else f"matching hash: {file_hash}"
            logging.info(f"Media '{filename}' already exists on SmugMug ({log_reason}).")
            # Clean up downloaded file if it exists (only images were downloaded at this stage)
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                logging.debug(f"Removed temporary file: {temp_file_path}")

            # Handle optional deletion from Google Photos
            if args.delete_from_google:
                print(f"Confirm deletion of '{filename}' from Google Photos? (yes/no): ", end="")
                confirmation = input().lower()
                if confirmation == 'yes':
                    if remove_from_google_photos(google_photos_service, media_item_id):
                        logging.info(f"Removed '{filename}' from Google Photos.")
                    else:
                        logging.warning(f"Failed to remove '{filename}' from Google Photos.")
                else:
                    logging.info(f"Skipping deletion of '{filename}' from Google Photos.")
            else:
                logging.info(
                    f"Skipping deletion of '{filename}' from Google Photos (use --delete-from-google to enable).")
        else:
            log_reason = "filename" if is_video else f"hash: {file_hash}"
            logging.info(f"Media '{filename}' ({log_reason}) not found on SmugMug. Proceeding with upload.")

            # Download video if it wasn't already downloaded for hash check
            if is_video:
                logging.debug(f"Downloading video '{filename}' for upload...")
                temp_file_path, _, _, _ = download_google_photo(google_photos_service, photo)
                if not temp_file_path:
                    logging.error(f"Skipping video '{filename}' due to download error during upload phase.")
                    continue

            # Ensure we have a temp_file_path before attempting upload
            if not temp_file_path:
                logging.error(f"Cannot upload '{filename}', temporary file path is missing.")
                continue

            # Upload the downloaded file (image or video) using the correct mime_type
            # The upload_to_smugmug function handles removing the temp_file_path in its finally block
            if upload_to_smugmug(smugmug_auth, album_api_uri, temp_file_path, filename, mime_type):
                logging.info(f"Successfully initiated transfer of '{filename}' to SmugMug.")
                # Optional: Consider deleting from Google Photos *after* successful upload?
                # This would require another command-line flag, e.g., --delete-after-upload
            else:
                logging.error(f"Failed to transfer '{filename}' to SmugMug.")
                # Note: upload_to_smugmug should have already tried to remove the temp file if it still exists


if __name__ == "__main__":
    main()