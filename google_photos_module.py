# Google Photos Module
#
# This module encapsulates all Google Photos-related functionality for the Google Photos to SmugMug Transfer Script.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
#
# Standard library imports
import logging
import os
import tempfile
import shutil

# Third-party imports
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Custom Exception for missing credentials
class GoogleCredentialsNotFoundError(Exception):
    """Custom exception for missing Google credentials file."""
    pass

class GooglePhotos:
    """Class encapsulating all Google Photos-related functionality."""

    # Google Photos API Scopes
    SCOPES = ['https://www.googleapis.com/auth/photoslibrary.readonly',
              'https://www.googleapis.com/auth/photoslibrary.appendonly'] # appendonly is needed for remove/delete

    def __init__(self, credentials_file='google_api_keys.json', token_file='google_photos_token.json',
                 batch_size=50):
        """Initialize with the paths to Google Photos configuration files."""
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.batch_size = batch_size
        self.service = None
        self.temp_dir = tempfile.mkdtemp(prefix="gp2sm_") # Create a unique temp dir for downloads

        # Try to authenticate during initialization, might raise GoogleCredentialsNotFoundError
        try:
            self.authenticate()
        except GoogleCredentialsNotFoundError:
            # Log message already printed by authenticate(), just re-raise to signal main.py
            raise
        except Exception as e:
             # Catch other potential authentication errors
             logging.error(f"An unexpected error occurred during Google Photos authentication: {e}")
             # Allow initialization to complete, but service will be None

    def is_authenticated(self):
        """Returns True if authenticated with Google Photos, False otherwise."""
        return self.service is not None

    def authenticate(self):
        """
        Authenticates with the Google Photos API using OAuth 2.0.
        Returns True if authentication is successful, False otherwise.
        Raises GoogleCredentialsNotFoundError if credentials file is missing.
        """
        creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(self.token_file, self.SCOPES)
            except Exception as e:
                logging.warning(f"Error loading Google Photos token from {self.token_file}: {e}. Will attempt re-authentication.")
                creds = None # Force re-authentication if token file is corrupt

        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Error refreshing Google Photos token: {e}. User may need to re-authorize.")
                    # Clear potentially invalid creds to force re-auth flow
                    creds = None
                    if os.path.exists(self.token_file):
                        try:
                            os.remove(self.token_file)
                            logging.info(f"Removed potentially invalid token file: {self.token_file}")
                        except OSError as rm_err:
                            logging.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")

            # If still no valid creds, try the flow from secrets file
            if not creds or not creds.valid:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.credentials_file, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                except FileNotFoundError:
                    logging.error(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    logging.error(f"Google Photos credentials file NOT FOUND: {self.credentials_file}")
                    logging.error(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    logging.error("Instructions to get the credentials file:")
                    logging.error("1. Go to the Google Cloud Console: https://console.cloud.google.com/")
                    logging.error("2. Create a new project or select an existing one.")
                    logging.error("3. Enable the 'Google Photos Library API' for your project.")
                    logging.error("4. Go to 'APIs & Services' -> 'Credentials'.")
                    logging.error("5. Click '+ CREATE CREDENTIALS' -> 'OAuth client ID'.")
                    logging.error("6. Select 'Desktop app' as the Application type.")
                    logging.error("7. Give it a name (e.g., 'gp2sm-script').")
                    logging.error("8. Click 'CREATE'. A pop-up will show your Client ID and Secret (you don't need to copy these now).")
                    logging.error("9. Find the newly created credential in the list and click the download icon (⬇️) to download the JSON file.")
                    logging.error(f"10. IMPORTANT: Rename the downloaded file to exactly '{self.credentials_file}' and place it in the same directory as the script.")
                    logging.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    print(f"\nERROR: Google Photos credentials file not found: '{self.credentials_file}'")
                    print("Please follow the instructions logged above to obtain and place the file.")
                    # Raise a specific exception to signal main.py to exit
                    raise GoogleCredentialsNotFoundError("Google Photos credentials file missing.")
                    # return False # Keep this line commented or remove, exception is better
                except Exception as flow_err:
                    logging.error(f"An error occurred during the Google OAuth flow: {flow_err}")
                    return False # General failure during auth flow

            # Save the credentials for the next run if they are valid
            if creds and creds.valid:
                try:
                    with open(self.token_file, 'w') as token:
                        token.write(creds.to_json())
                except IOError as e:
                    logging.error(f"Error saving Google Photos token to {self.token_file}: {e}")
            else:
                 logging.error("Failed to obtain valid Google Photos credentials after authorization flow.")
                 return False

        # Build the service if credentials are valid
        if creds and creds.valid:
            try:
                self.service = build('photoslibrary', 'v1', credentials=creds, static_discovery=False)
                logging.info("Successfully authenticated with Google Photos API.")
                # Optional: Verify credentials by making a small API call
                self.service.mediaItems().list(pageSize=1).execute()
                logging.debug("Google Photos API credentials verified.")
                return True
            except HttpError as error:
                logging.error(f'An HTTP error occurred during Google Photos API authentication or verification: {error}')
                self.service = None  # Ensure service is None if verification fails
                return False
            except Exception as e:
                logging.error(f'An unexpected error occurred during Google Photos API authentication: {e}')
                self.service = None
                return False
        else:
            logging.error("Could not establish valid Google Photos credentials.")
            self.service = None
            return False

    def get_photos(self, album_id=None):
        """Retrieves a list of photos from the Google Photos library, optionally from a specific album."""
        if not self.is_authenticated():
            logging.error("Not authenticated with Google Photos. Cannot retrieve photos.")
            return []

        photos = []
        nextPageToken = None
        # Construct the body of the request. This is required for both library and album searches.
        body = {
            'pageSize': self.batch_size,
        }
        if album_id:
            body['albumId'] = album_id
            logging.info(f"Configured to fetch photos only from Google Photos album ID: {album_id}")
        else:
             logging.info("Configured to fetch all photos from Google Photos library.")

        # Determine which method to use based on album_id
        method_name = 'search' if album_id else 'list'
        method = getattr(self.service.mediaItems(), method_name)

        total_items_retrieved = 0  # Keep track of total items retrieved across all pages

        while True:
            try:
                if album_id:
                    # Search method requires the body payload
                    if nextPageToken: body['pageToken'] = nextPageToken # Add token for subsequent pages
                    logging.debug(f"Fetching page for album {album_id} with body: {body}")
                    results = method(body=body).execute()
                    items = results.get('mediaItems')
                else:
                    # List method takes parameters directly
                    logging.debug(f"Fetching page for library list with pageSize={self.batch_size}, pageToken={nextPageToken}")
                    results = method(pageSize=self.batch_size, pageToken=nextPageToken).execute()
                    items = results.get('mediaItems')

                if not items:
                    if nextPageToken:  # If there was a token but no items, it's the end
                        logging.info("No more items found with the current page token.")
                    elif not photos: # No token and no photos retrieved yet means empty library/album
                        logging.info("No photos found in the specified Google Photos location.")
                    else: # No token, but photos were retrieved on previous pages, so this is the end
                         logging.info("Finished retrieving all items.")
                    break

                photos.extend(items)
                num_items_in_batch = len(items)
                total_items_retrieved += num_items_in_batch
                logging.info(f"Retrieved {num_items_in_batch} items in this batch. Total items retrieved so far: {total_items_retrieved}")

                # Pagination: next page token might be in the results for list *or* search
                nextPageToken = results.get('nextPageToken')
                if not nextPageToken:
                    break  # No more pages

            except HttpError as error:
                logging.error(f'An error occurred while retrieving photos from Google Photos: {error}')
                break
            except Exception as e:
                logging.error(f'An unexpected error occurred while retrieving photos from Google Photos: {e}')
                break  # Exit loop on unexpected error as well

        logging.info(f"Finished retrieving items. Total items found: {len(photos)}")
        return photos

    def download_photo(self, item):
        """Downloads a photo/video item to the temporary directory."""
        if not self.is_authenticated():
            logging.error("Not authenticated with Google Photos. Cannot download.")
            return None, None, None, None, None

        media_item_id = item['id']
        filename = item.get('filename', f"unknown_{media_item_id}")
        mime_type = item.get('mimeType', 'application/octet-stream')
        base_url = item.get('baseUrl')
        is_video = mime_type.startswith('video/')

        if not base_url:
            logging.error(f"Cannot download '{filename}' ({media_item_id}): Missing baseUrl.")
            return None, None, None, None, None

        # Determine the download URL parameter based on type
        download_param = "=dv" if is_video else "=d"
        download_url = base_url + download_param

        # Create a unique temporary file path within the instance's temp_dir
        # Using mkstemp for security and uniqueness
        fd, temp_file_path = tempfile.mkstemp(suffix=f"_{filename}", dir=self.temp_dir)
        os.close(fd) # Close the file descriptor, we'll open in 'wb'

        logging.debug(f"Attempting to download '{filename}' from {download_url} to {temp_file_path}")

        try:
            # Use stream=True for potentially large files
            response = requests.get(download_url, stream=True)
            response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)

            with open(temp_file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192): # Download in chunks
                    f.write(chunk)

            logging.debug(f"Successfully downloaded '{filename}' to {temp_file_path}")

            # Get metadata if available (might be approximate for videos after download)
            width = item.get('mediaMetadata', {}).get('width', 0)
            height = item.get('mediaMetadata', {}).get('height', 0)

            return temp_file_path, filename, width, height, mime_type

        except requests.exceptions.RequestException as e:
            logging.error(f"Error downloading '{filename}' ({media_item_id}): {e}")
            # Clean up partial download
            if os.path.exists(temp_file_path):
                try: os.remove(temp_file_path)
                except OSError: pass
            return None, None, None, None, None
        except IOError as e:
            logging.error(f"Error writing downloaded file '{filename}' to {temp_file_path}: {e}")
            # Clean up partial download
            if os.path.exists(temp_file_path):
                try: os.remove(temp_file_path)
                except OSError: pass
            return None, None, None, None, None

    def remove_photo(self, media_item_id, dry_run=False):
        """Removes a photo from Google Photos by its ID (requires appendonly scope)."""
        # NOTE: Google Photos API currently does NOT support direct deletion.
        # This function simulates removal by logging.
        # If the API adds deletion in the future, this is where the call would go.
        if not self.is_authenticated():
             logging.error(f"Not authenticated. Cannot {'simulate removing' if dry_run else 'remove'} {media_item_id}.")
             return False

        logging.warning("Google Photos API currently does not support direct deletion of media items.")
        log_prefix = "[DRY RUN] Would remove" if dry_run else "[API UNSUPPORTED] Would remove"
        logging.info(f"{log_prefix} media item {media_item_id} from Google Photos if the API supported it.")
        # In a real scenario, you would make an API call here if it existed.
        # For now, we return True to indicate the simulation/logging was successful.
        return True

    def __del__(self):
        """Clean up the temporary directory when the object is destroyed."""
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logging.debug(f"Cleaned up temporary directory: {self.temp_dir}")
            except OSError as e:
                logging.warning(f"Could not remove temporary directory {self.temp_dir}: {e}")
