import os
import json
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import logging
import webbrowser
from requests_oauthlib import OAuth1Session
import argparse
import hashlib
import hmac
import base64
import time
import uuid
import urllib.parse

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


if __name__ == '__main__':
    # This is just an example of how you might call this function
    # You would likely integrate it into your main script
    api_key = "YOUR_SMUGMUG_API_KEY"  # Replace with your API key
    api_secret = "YOUR_SMUGMUG_API_SECRET"  # Replace with your API secret

    if api_key == "YOUR_SMUGMUG_API_KEY" or api_secret == "YOUR_SMUGMUG_API_SECRET":
        print("Please configure your SmugMug API key and secret in this block or in your config file.")
    else:
        token, secret = obtain_smugmug_oauth_tokens(api_key, api_secret)
        if token and secret:
            print(f"OAuth Token: {token}")
            print(f"OAuth Token Secret: {secret}")
        else:
            print("Failed to obtain SmugMug OAuth tokens.")

def get_smugmug_auth():
    """Returns an OAuth1 object for SmugMug API authentication, obtaining tokens if needed."""
    smugmug_config = load_smugmug_config()
    if not smugmug_config:
        return None

    api_key = smugmug_config.get('api_key')
    api_secret = smugmug_config.get('api_secret')
    oauth_token = smugmug_config.get('oauth_token')
    oauth_token_secret = smugmug_config.get('oauth_token_secret')

    if not api_key or not api_secret:
        logging.error("SmugMug API Key and Secret must be configured in smugmug_config.json.")
        return None

    if oauth_token == "YOUR_SMUGMUG_OAUTH_TOKEN" or not oauth_token or not oauth_token_secret:
        logging.info("SmugMug OAuth tokens not found in config. Initiating authorization flow...")
        new_oauth_token, new_oauth_token_secret = obtain_smugmug_oauth_tokens(api_key, api_secret)
        if new_oauth_token and new_oauth_token_secret:
            smugmug_config['oauth_token'] = new_oauth_token
            smugmug_config['oauth_token_secret'] = new_oauth_token_secret
            save_smugmug_config(smugmug_config)
            oauth_token = new_oauth_token
            oauth_token_secret = new_oauth_token_secret
        else:
            logging.error("Failed to obtain SmugMug OAuth tokens. Please check the logs and try again.")
            return None

    auth = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token,
                         resource_owner_secret=oauth_token_secret)
    logging.info("SmugMug API authentication configured.")
    return auth


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
    import hashlib  # Import hashlib here to ensure it's available
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


def check_photo_exists_smugmug(auth, filename, file_hash=None, min_size_bytes=None, max_size_bytes=None):
    """Checks if a photo exists on SmugMug, primarily by hash if feasible, otherwise by filename and size."""
    smugmug_config = load_smugmug_config()
    if not smugmug_config:
        return False
    album_key = smugmug_config.get('album_key')

    if not album_key:
        logging.warning(
            "No SmugMug Album Key specified in smugmug_config.json. Existence check might be less efficient or skipped.")
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
                for image in data['Response']['AlbumImage']:
                    if image.get('FileName') == filename:
                        if file_hash:
                            # Ideally, we'd have a hash from SmugMug to compare
                            logging.warning("Hash-based checking not fully implemented for SmugMug.")
                            return True  # For now, if filename matches and hash is provided, consider it a match
                        elif min_size_bytes and max_size_bytes:
                            image_size = image.get('OriginalSize')
                            if image_size is not None and min_size_bytes <= image_size <= max_size_bytes:
                                return True
                        else:
                            return True  # Filename matches and no size constraints

            if 'NextPage' in data['Response']['Pages']:
                next_page_url = data['Response']['Pages']['NextPage']
            else:
                next_page_url = None

        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking for photo existence on SmugMug: {e}")
            return False

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
        return file_path, filename, response.get('mediaMetadata', {}).get('width'), response.get('mediaMetadata',
                                                                                                 {}).get('height')
    except HttpError as error:
        logging.error(
            f'An error occurred while downloading photo {media_item.get("filename", media_item["id"])} from Google Photos: {error}')
        return None, None, None, None
    except requests.exceptions.RequestException as e:
        logging.error(
            f'An error occurred during the download request for photo {media_item.get("filename", media_item["id"])}: {e}')
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


def upload_to_smugmug(auth_session, album_api_uri, file_path, filename, image_type='image/jpeg'):
    """Upload image to specified Album API URI, given an authenticated session and image file path."""
    try:
        with open(file_path, 'rb') as image_file:
            image_data = image_file.read()

        headers = {
            'Accept': b'application/json',
            'Content-Length': str(len(image_data)),
            'Content-MD5': hashlib.md5(image_data).hexdigest(),
            'Content-Type': image_type,
            'X-Smug-AlbumUri': album_api_uri,
            'X-Smug-FileName': filename,
            'X-Smug-ResponseType': 'JSON',
            'X-Smug-Version': 'v2',
        }

        response = auth_session.post('https://upload.smugmug.com/', headers=headers, data=image_data)
        response.raise_for_status()
        upload_data = response.json()
        if 'stat' in upload_data and upload_data['stat'] == 'ok':
            logging.info(f"Successfully uploaded to SmugMug: {upload_data.get('Image', {}).get('URL', 'Unknown URL')}")
            return True
        else:
            logging.error(f"Failed to upload {filename} to SmugMug. Response: {upload_data}")
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


def not_main():
    smugmug_auth = get_smugmug_auth()
    if not smugmug_auth:
        return

    headers = {'Accept': 'application/json'}
    try:
        response = smugmug_auth.get('https://api.smugmug.com/api/v2/user/me', headers=headers)
        response.raise_for_status()
        user_data = response.json()
        print("Successfully fetched user data:")
        print(json.dumps(user_data, indent=2))
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching user data from SmugMug: {e}")
        print(f"Response Text: {response.text}")

def main():
    """Main function to orchestrate the photo transfer."""
    parser = argparse.ArgumentParser(description="Transfer photos from Google Photos to SmugMug.")
    parser.add_argument('--delete-from-google', action='store_true',
                        help='Delete photos from Google Photos if they exist on SmugMug (requires confirmation).')
    parser.add_argument('--google-photos-album-id', type=str,
                        help='Process photos only from the specified Google Photos album ID.')
    args = parser.parse_args()

    google_photos_service = authenticate_google_photos()
    if not google_photos_service:
        return

    smugmug_config = load_smugmug_config()
    smugmug_auth = get_smugmug_auth()
    if not smugmug_auth:
        return

    google_photos = get_google_photos(google_photos_service, args.google_photos_album_id)
    total_photos = len(google_photos)
    processed_count = 0

    for photo in google_photos:
        processed_count += 1
        filename = photo.get('filename')
        media_item_id = photo['id']
        file_size = photo.get('mediaMetadata', {}).get('fileSize')
        min_size = int(file_size) - 100 if file_size else None  # Add a small buffer for size comparison
        max_size = int(file_size) + 100 if file_size else None

        logging.info(f"Processing photo {processed_count}/{total_photos}: {filename} ({media_item_id})")

        # Check if photo exists on SmugMug
        exists_on_smugmug = check_photo_exists_smugmug(smugmug_auth, filename, min_size_bytes=min_size,
                                                       max_size_bytes=max_size)

        if exists_on_smugmug:
            logging.info(f"Photo '{filename}' already exists on SmugMug.")
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
            logging.info(f"Photo '{filename}' not found on SmugMug. Downloading and uploading.")
            file_path, downloaded_filename, _, _ = download_google_photo(google_photos_service, photo)
            if file_path:
                # if upload_to_smugmug(smugmug_auth, file_path, downloaded_filename):
                if upload_to_smugmug(smugmug_auth, smugmug_config.get('album_api_uri'), file_path, filename, 'image/jpeg'):
                    logging.info(f"Successfully transferred '{downloaded_filename}' to SmugMug.")
                    # No deletion here as it was not on SmugMug before
                else:
                    logging.error(f"Failed to transfer '{downloaded_filename}' to SmugMug.")


if __name__ == "__main__":
    main()

# --- Instructions on Obtaining SmugMug OAuth Tokens ---
"""
To use this script, you need to obtain your SmugMug API Key and Secret by registering your application on the SmugMug Developer Portal (https://api.smugmug.com/api/developer/apply).

The script will now attempt to obtain the OAuth Access Token and Secret automatically during the first run (or whenever they are not found in the smugmug_config.json file). You will be prompted to authorize the application in your web browser.

Follow these steps:

1. Ensure you have your SmugMug API Key and Secret configured in the smugmug_config.json file.
2. Run the Python script.
3. The script will print a URL to the console and attempt to open it in your web browser.
4. Log in to your SmugMug account if you are not already logged in.
5. Authorize the application to access your SmugMug account.
6. After authorizing, SmugMug will provide you with a verifier code. Copy this code.
7. Return to the terminal where the script is running and paste the verifier code when prompted.
8. The script will then exchange the request token for an access token and secret, and these will be saved in your smugmug_config.json file for future use.

On subsequent runs, the script will load the saved OAuth tokens from the configuration file and you will not need to log in again unless the tokens are revoked.
"""

# --- Instructions for Google Photos Authentication ---
"""
To use Google Photos with this script, you need to:

1. Create or select a project in the Google Cloud Console (https://console.cloud.google.com/).
2. Enable the Google Photos Library API for your project.
3. Create OAuth 2.0 credentials for your project:
   - Go to "APIs & Services" > "Credentials".
   - Click "Create credentials" and choose "OAuth client ID".
   - Select "Desktop application" as the application type.
   - Give it a name and click "Create".
4. Download the JSON file containing your client ID and client secret.
5. Rename the downloaded file to 'google_photos_credentials.json' and place it in the same directory as your Python script.

When you run the script for the first time, it will:

1. Check if a 'google_photos_token.json' file exists. This file stores your access and refresh tokens.
2. If the token file doesn't exist or the existing token is invalid, the script will attempt to open a web browser.
3. You will be prompted to log in to your Google account and grant permission for the script to access your Google Photos library.
4. After you grant permission, the script will save the authentication tokens in 'google_photos_token.json' for future use.

On subsequent runs, the script will automatically load the tokens from this file, and you won't need to log in again unless the tokens are revoked or expired.
"""

# --- Instructions for Running with Command Line Arguments ---
"""
You can run the script with the following optional command line arguments:

--delete-from-google: This flag, when present, will enable the deletion of photos from Google Photos if a matching photo (by filename and size) is found on SmugMug. You will be prompted for confirmation before each deletion.

--google-photos-album-id <album_id>: If you want to process photos from a specific Google Photos album, provide the ID of that album with this argument. You can find the album ID in the URL of your Google Photos album. For example, in 'https://photos.google.com/album/ALBUM_ID', 'ALBUM_ID' is the ID you need.

Example usage:

# Run the script without deleting and processing all photos
python your_script_name.py

# Run the script and delete photos from Google Photos after confirmation
python your_script_name.py --delete-from-google

# Run the script and process photos only from a specific Google Photos album
python your_script_name.py --google-photos-album-id YOUR_ALBUM_ID

# Run the script to process a specific album and enable deletion with confirmation
python your_script_name.py --google-photos-album-id YOUR_ALBUM_ID --delete-from-google
"""