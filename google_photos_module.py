# Google Photos Module
#
# This module encapsulates all Google Photos-related functionality for the Google Photos to SmugMug Transfer Script.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
#

# Standard library imports
import os
import json
import logging

# Third-party imports
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class GooglePhotos:
    """Class encapsulating all Google Photos-related functionality."""
    
    # Google Photos API Scopes
    SCOPES = ['https://www.googleapis.com/auth/photoslibrary.readonly',
              'https://www.googleapis.com/auth/photoslibrary.appendonly']
    
    def __init__(self, credentials_file='google_photos_credentials.json', token_file='google_photos_token.json', batch_size=50):
        """Initialize with the paths to Google Photos configuration files."""
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.batch_size = batch_size
        self.service = None
        
        # Try to authenticate during initialization
        self.authenticate()
    
    def is_authenticated(self):
        """Returns True if authenticated with Google Photos, False otherwise."""
        return self.service is not None
    
    def authenticate(self):
        """
        Authenticates with the Google Photos API using OAuth 2.0.
        Returns True if authentication is successful, False otherwise.
        """
        creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, self.SCOPES)
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.credentials_file, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                except FileNotFoundError:
                    logging.error(f"Google Photos credentials file not found: {self.credentials_file}. Please ensure it exists.")
                    return False
            # Save the credentials for the next run
            try:
                with open(self.token_file, 'w') as token:
                    token.write(creds.to_json())
            except IOError as e:
                logging.error(f"Error saving Google Photos token to {self.token_file}: {e}")

        try:
            self.service = build('photoslibrary', 'v1', credentials=creds,
                            discoveryServiceUrl='https://photoslibrary.googleapis.com/$discovery/rest?version=v1')
            logging.info("Successfully authenticated with Google Photos API.")
            # Optional: Verify credentials by making a small API call
            self.service.mediaItems().list(pageSize=1).execute()
            logging.debug("Google Photos API credentials verified.")
            return True
        except HttpError as error:
            logging.error(f'An HTTP error occurred during Google Photos API authentication or verification: {error}')
            self.service = None # Ensure service is None if verification fails
            return False
        except Exception as e:
            logging.error(f'An unexpected error occurred during Google Photos API authentication: {e}')
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

        # Determine which method to use based on album_id
        method = self.service.mediaItems().search if album_id else self.service.mediaItems().list

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
                    results = method(pageSize=self.batch_size, pageToken=nextPageToken).execute()
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
    
    def download_photo(self, media_item):
        """Downloads a media item (photo or video) from Google Photos in its original quality."""
        if not self.is_authenticated():
            logging.error("Not authenticated with Google Photos. Cannot download photo.")
            return None, None, None, None, None
            
        media_item_id = media_item['id']
        filename = media_item.get('filename', f"media_{media_item_id}.dat") # Add a default extension
        mime_type = media_item.get('mimeType', '')
        is_video = mime_type.startswith('video/')

        try:
            # Use the baseUrl directly from the media item list response if available,
            # or fetch full details if needed (though baseUrl is usually present).
            # Fetching details again is safer if the initial list response is minimal.
            item_details = self.service.mediaItems().get(mediaItemId=media_item_id).execute()
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
    
    def remove_photo(self, media_item_id, dry_run=False):
        """Removes a photo from Google Photos."""
        if not self.is_authenticated():
            logging.error("Not authenticated with Google Photos. Cannot remove photo.")
            return False
            
        if dry_run:
            logging.info(f"[DRY RUN] Would remove photo from Google Photos: {media_item_id}")
            return True # Simulate success in dry run
        try:
            # The batchRemove endpoint expects a list of IDs
            response = self.service.mediaItems().batchRemove(mediaItemIds=[media_item_id]).execute()
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