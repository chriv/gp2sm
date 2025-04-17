import os
import json
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from hashlib import sha256
import logging
from requests_oauthlib import OAuth1

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration Files ---
GOOGLE_PHOTOS_CREDENTIALS_FILE = 'google_photos_credentials.json'  # Path to your Google Photos API credentials file
SMUGMUG_CONFIG_FILE = 'smugmug_config.json'  # Path to your SmugMug API configuration file
BATCH_SIZE = 50  # Number of photos to process in each batch

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
    """Authenticates with the Google Photos API."""
    try:
        with open(GOOGLE_PHOTOS_CREDENTIALS_FILE, 'r') as f:
            creds_data = json.load(f)
    except FileNotFoundError:
        logging.error(f"Google Photos credentials file not found: {GOOGLE_PHOTOS_CREDENTIALS_FILE}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON from Google Photos credentials file: {GOOGLE_PHOTOS_CREDENTIALS_FILE}")
        return None

    creds = Credentials.from_authorized_user_info(creds_data, scopes=['https://www.googleapis.com/auth/photoslibrary.readonly', 'https://www.googleapis.com/auth/photoslibrary.appendonly', 'https://www.googleapis.com/auth/photoslibrary.edit.appcreated'])
    try:
        service = build('photoslibrary', 'v1', credentials=creds)
        logging.info("Successfully authenticated with Google Photos API.")
        return service
    except HttpError as error:
        logging.error(f'An error occurred during Google Photos API authentication: {error}')
        return None

def get_smugmug_auth():
    """Returns an OAuth1 object for SmugMug API authentication."""
    smugmug_config = load_smugmug_config()
    if not smugmug_config:
        return None

    api_key = smugmug_config.get('api_key')
    api_secret = smugmug_config.get('api_secret')
    oauth_token = smugmug_config.get('oauth_token')
    oauth_token_secret = smugmug_config.get('oauth_token_secret')

    if not api_key or not api_secret or not oauth_token or not oauth_token_secret:
        logging.error("SmugMug API Key, Secret, OAuth Token, and Token Secret must be configured in smugmug_config.json.")
        return None

    auth = OAuth1(api_key, api_secret, oauth_token, oauth_token_secret)
    logging.info("SmugMug API authentication configured.")
    return auth

def get_google_photos(service):
    """Retrieves a list of photos from the Google Photos library."""
    photos = []
    nextPageToken = None
    while True:
        try:
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
    hasher = sha256()
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

def check_photo_exists_smugmug(auth, filename, file_hash=None, min_size_bytes=None, max_size_bytes=None):
    """Checks if a photo exists on SmugMug, primarily by hash if feasible, otherwise by filename and size."""
    smugmug_config = load_smugmug_config()
    if not smugmug_config:
        return False
    album_key = smugmug_config.get('album_key')

    if not album_key:
        logging.warning("No SmugMug Album Key specified in smugmug_config.json. Existence check might be less efficient or skipped.")
        return False

    album_url = f'https://api.smugmug.com/api/v2/album/{album_key}!images'
    next_page_url = album_url

    try:
        while next_page_url:
            response = requests.get(next_page_url, auth=auth)
            response.raise_for_status()
            data = response.json()

            if 'Response' in data and 'AlbumImage' in data['Response']:
                for image in data['Response']['AlbumImage']:
                    if image.get('FileName') == filename:
                        if file_hash:
                            logging.warning("Direct file hash comparison with SmugMug API might not be feasible without downloading the image.")
                            return False # For safety, assuming not found if hash is provided and direct comparison isn't available
                        elif min_size_bytes is not None and max_size_bytes is not None and image.get('Size') is not None:
                            if min_size_bytes <= image['Size'] <= max_size_bytes:
                                logging.info(f"Found potential duplicate on SmugMug by filename and size: {filename}")
                                return True
                        elif min_size_bytes is None and max_size_bytes is None:
                            logging.info(f"Found potential duplicate on SmugMug by filename: {filename}")
                            return True

            if 'NextPage' in data['Response']['Pages']:
                next_page_url = data['Response']['Pages']['NextPage']
            else:
                next_page_url = None

        return False

    except requests.exceptions.RequestException as e:
        logging.error(f"Error checking photo existence on SmugMug: {e}")
        return False

def download_google_photo(service, media_item):
    """Downloads a photo from Google Photos in its original quality."""
    try:
        response = service.mediaItems().get(mediaItemId=media_item['id']).execute()
        download_url = response.get('baseUrl') + '=d'  # '=d' forces download
        image_data = requests.get(download_url)
        image_data.raise_for_status()  # Raise an exception for bad status codes

        filename = media_item.get('filename', f"photo_{media_item['id']}")
        file_path = f"temp_{filename}"
        with open(file_path, 'wb') as f:
            f.write(image_data.content)
        logging.info(f"Downloaded photo from Google Photos: {filename}")
        return file_path, filename, response.get('mediaMetadata', {}).get('width'), response.get('mediaMetadata', {}).get('height')
    except HttpError as error:
        logging.error(f'An error occurred while downloading photo {media_item.get("filename", media_item["id"])} from Google Photos: {error}')
        return None, None, None, None
    except requests.exceptions.RequestException as e:
        logging.error(f'An error occurred during the download request for photo {media_item.get("filename", media_item["id"])}: {e}')
        return None, None, None, None

def upload_to_smugmug(auth, file_path, filename):
    """Uploads a photo to SmugMug."""
    smugmug_config = load_smugmug_config()
    if not smugmug_config:
        return False
    album_key = smugmug_config.get('album_key')

    if not album_key:
        logging.error("Cannot upload to SmugMug without a specified Album Key in smugmug_config.json.")
        return False

    upload_url = f'https://upload.smugmug.com/'  # This might need to be adjusted based on the SmugMug API documentation
    try:
        with open(file_path, 'rb') as img_file:
            files = {'image': (filename, img_file)}
            params = {'AlbumID': album_key, 'Filename': filename}
            response = requests.post(upload_url, auth=auth, files=files, params=params)
            response.raise_for_status()
            upload_data = response.json()
            if 'stat' in upload_data and upload_data['stat'] == 'ok':
                logging.info(f"Successfully uploaded to SmugMug: {filename}")
                return True
            else:
                logging.error(f"Failed to upload {filename} to SmugMug. Response: {upload_data}")
                return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error uploading {filename} to SmugMug: {e}")
        return False
    finally:
        # Clean up the temporary file
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
    google_photos_service = authenticate_google_photos()
    if not google_photos_service:
        return

    smugmug_auth = get_smugmug_auth()
    if not smugmug_auth:
        return

    google_photos = get_google_photos(google_photos_service)
    total_photos = len(google_photos)
    processed_count = 0

    for photo in google_photos:
        processed_count += 1
        filename = photo.get('filename')
        media_item_id = photo['id']
        file_size = photo.get('mediaMetadata', {}).get('fileSize')
        min_size = int(file_size) - 100 if file_size else None # Add a small buffer for size comparison
        max_size = int(file_size) + 100 if file_size else None

        logging.info(f"Processing photo {processed_count}/{total_photos}: {filename} ({media_item_id})")

        # Check if photo exists on SmugMug
        exists_on_smugmug = check_photo_exists_smugmug(smugmug_auth, filename, min_size_bytes=min_size, max_size_bytes=max_size)

        if exists_on_smugmug:
            logging.info(f"Photo '{filename}' already exists on SmugMug. Removing from Google Photos.")
            remove_from_google_photos(google_photos_service, media_item_id)
        else:
            logging.info(f"Photo '{filename}' not found on SmugMug. Downloading and uploading.")
            file_path, downloaded_filename, _, _ = download_google_photo(google_photos_service, photo)
            if file_path:
                if upload_to_smugmug(smugmug_auth, file_path, downloaded_filename):
                    logging.info(f"Successfully transferred '{downloaded_filename}' to SmugMug.")
                    # Optionally remove from Google Photos after successful transfer
                    # remove_from_google_photos(google_photos_service, media_item_id)
                else:
                    logging.error(f"Failed to transfer '{downloaded_filename}' to SmugMug.")

if __name__ == "__main__":
    main()

# --- Instructions on Obtaining SmugMug OAuth Tokens ---
"""
To use this script, you need to obtain your SmugMug API Key, Secret, OAuth Token, and OAuth Token Secret. SmugMug uses OAuth 1.0a for authentication. Here's a general outline of how to get these:

1. Register Your Application with SmugMug:
   - Go to the SmugMug Developer Portal (https://api.smugmug.com/api/developer/apply) and register your application. You'll receive your API Key and API Secret.

2. Obtain an OAuth Request Token:
   - You'll need to make an API call to SmugMug to get a request token. This usually involves using your API Key and Secret. Libraries like 'requests-oauthlib' can help with this step.

3. Redirect the User for Authorization:
   - Once you have a request token, you need to redirect the user to a SmugMug authorization URL where they can grant your application permission to access their account. This URL will include your request token.

4. Obtain the OAuth Access Token:
   - After the user authorizes your application, SmugMug will redirect them back to a callback URL you specified during registration (or they might provide a verifier code). You'll then exchange the request token (and potentially the verifier code) for a permanent OAuth Access Token and Token Secret.

The exact steps and API endpoints for OAuth 1.0a with SmugMug can be found in their official API documentation. You might need to use a separate script or tool to go through the initial authorization process and obtain your OAuth Token and Secret. Once you have these, you can configure them in the 'smugmug_config.json' file.

Alternatively, you might find tools or online resources that can help you generate your SmugMug OAuth Token and Secret.

Remember to keep your API Key, Secret, OAuth Token, and Token Secret secure.
"""