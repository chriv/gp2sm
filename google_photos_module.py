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
import time # Needed for sleep
import datetime # Needed for expiry check

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

# Get the logger instance
logger = logging.getLogger(__name__)

class GooglePhotos:
    """Class encapsulating all Google Photos-related functionality."""

    # Google Photos API Scopes
    SCOPES = ['https://www.googleapis.com/auth/photoslibrary.readonly',
              'https://www.googleapis.com/auth/photoslibrary.appendonly'] # appendonly might be needed later for delete/remove

    def __init__(self, credentials_file='google_api_keys.json', token_file='google_photos_token.json',
                 batch_size=50):
        """Initialize with the paths to Google Photos configuration files."""
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.batch_size = batch_size
        self.service = None
        self.creds = None # Store credentials object
        self.temp_dir = None
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="gp2sm_")
            logger.debug(f"Created temporary directory: {self.temp_dir}")
        except Exception as e:
             logger.error(f"Failed to create temporary directory: {e}", exc_info=True)
             self.temp_dir = None

        try:
            self.authenticate()
        except GoogleCredentialsNotFoundError:
            raise
        except Exception as e:
             logger.error(f"An unexpected error occurred during Google Photos authentication: {e}", exc_info=True)

    def is_authenticated(self):
        """Returns True if authenticated with Google Photos, False otherwise."""
        # Check both service and credentials validity
        return self.service is not None and self.creds and self.creds.valid

    def authenticate(self):
        """
        Authenticates with the Google Photos API using OAuth 2.0.
        Stores credentials in self.creds.
        Returns True if authentication is successful, False otherwise.
        Raises GoogleCredentialsNotFoundError if credentials file is missing.
        """
        creds = None
        if os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(self.token_file, self.SCOPES)
                logger.debug(f"Loaded credentials from {self.token_file}")
            except Exception as e:
                logger.warning(f"Error loading Google Photos token from {self.token_file}: {e}. Will attempt re-authentication.")
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Google Photos token expired. Attempting refresh...")
                try:
                    creds.refresh(Request())
                    logger.info("Token refreshed successfully.")
                    logger.info(f"Saving refreshed Google Photos token to: {self.token_file}")
                    try:
                        with open(self.token_file, 'w') as token:
                            token.write(creds.to_json())
                    except IOError as e:
                        logger.error(f"Error saving refreshed Google Photos token to {self.token_file}: {e}")
                except Exception as e:
                    logger.error(f"Error refreshing Google Photos token: {e}. User may need to re-authorize.")
                    creds = None
                    if os.path.exists(self.token_file):
                        try: os.remove(self.token_file); logger.info(f"Removed potentially invalid token file: {self.token_file}")
                        except OSError as rm_err: logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")

            if not creds or not creds.valid:
                logger.info("No valid Google Photos credentials found, initiating authorization flow...")
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                    logger.info("Authorization flow completed.")
                    logger.info(f"Saving new Google Photos token to: {self.token_file}")
                    try:
                        with open(self.token_file, 'w') as token:
                            token.write(creds.to_json())
                    except IOError as e:
                        logger.error(f"Error saving new Google Photos token to {self.token_file}: {e}")
                except FileNotFoundError:
                    logger.error(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    logger.error(f"Google Photos credentials file NOT FOUND: {self.credentials_file}")
                    logger.error(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    logger.error("...") # Keep instructions
                    logger.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                    raise GoogleCredentialsNotFoundError("Google Photos credentials file missing.")
                except Exception as flow_err:
                    logger.error(f"An error occurred during the Google OAuth flow: {flow_err}", exc_info=True)
                    self.creds = None # Ensure creds is None on failure
                    return False

            if not creds or not creds.valid:
                 logger.error("Failed to obtain valid Google Photos credentials after authorization attempt.")
                 self.creds = None # Ensure creds is None on failure
                 return False

        # Store the valid credentials object
        self.creds = creds

        # Build the service
        try:
            self.service = build('photoslibrary', 'v1', credentials=self.creds, static_discovery=False)
            logger.info("Successfully built Google Photos API service.")
            logger.debug("Verifying Google Photos API credentials...")
            self.service.mediaItems().list(pageSize=1).execute() # Verification call
            logger.debug("Google Photos API credentials verified.")
            return True
        except HttpError as error:
            logger.error(f'An HTTP error occurred during Google Photos API service build or verification: {error}')
            self.service = None
            self.creds = None # Invalidate creds if service build fails
            return False
        except Exception as e:
            logger.error(f'An unexpected error occurred during Google Photos API service build: {e}', exc_info=True)
            self.service = None
            self.creds = None
            return False

    def refresh_token_if_needed(self, buffer_minutes=10):
        """Checks if the token is close to expiry and refreshes it."""
        if not self.creds or not self.creds.valid:
            logger.debug("Token refresh check skipped: Credentials not valid or missing.")
            return True # Nothing to do, technically not a failure

        if not self.creds.expiry:
             logger.debug("Token refresh check skipped: No expiry information available.")
             return True # Cannot check expiry

        # Ensure 'now' is timezone-aware (UTC) to compare with expiry
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        # Calculate refresh time (expiry - buffer)
        refresh_time = self.creds.expiry - datetime.timedelta(minutes=buffer_minutes)

        if now_utc >= refresh_time:
            logger.info(f"Google Photos token expires soon (at {self.creds.expiry}). Refreshing proactively...")
            try:
                self.creds.refresh(Request())
                logger.info("Token refreshed successfully via proactive check.")
                # Save the updated token
                logger.info(f"Saving proactively refreshed Google Photos token to: {self.token_file}")
                try:
                    with open(self.token_file, 'w') as token:
                        token.write(self.creds.to_json())
                    return True
                except IOError as e:
                    logger.error(f"Error saving proactively refreshed Google Photos token to {self.token_file}: {e}")
                    return False # Failed to save refreshed token
            except Exception as e:
                logger.error(f"Error during proactive token refresh: {e}. Re-authentication might be needed later.")
                # Mark credentials as potentially invalid? Or let the next API call fail?
                # Let's return False to indicate refresh failed.
                return False
        else:
            logger.debug(f"Token refresh check: Token valid until {self.creds.expiry}. No proactive refresh needed yet.")
            return True # No refresh needed

    def get_photos(self, album_id=None):
        """Retrieves a list of photos from the Google Photos library, optionally from a specific album."""
        if not self.is_authenticated():
            logger.error("Not authenticated with Google Photos. Cannot retrieve photos.")
            # Attempt re-authentication once if service exists but creds are invalid
            if self.service is None and self.creds is None:
                 if not self.authenticate():
                      logger.error("Re-authentication failed.")
                      return []
            elif not self.creds or not self.creds.valid:
                 if not self.refresh_token_if_needed(buffer_minutes=0): # Force refresh attempt
                      if not self.authenticate(): # Try full auth if refresh failed
                           logger.error("Re-authentication failed after refresh failure.")
                           return []
                 # Recheck after potential refresh/auth
                 if not self.is_authenticated():
                       logger.error("Still not authenticated after re-attempt.")
                       return []


        all_items = []
        nextPageToken = None
        page_count = 0
        total_items_retrieved = 0

        # Construct the body of the request only if searching within an album
        body = {'albumId': album_id, 'pageSize': self.batch_size} if album_id else {}

        method_name = 'search' if album_id else 'list'
        method = getattr(self.service.mediaItems(), method_name)

        action_desc = f"album ID: {album_id}" if album_id else "library"
        logger.info(f"Starting to fetch media items from Google Photos {action_desc}...")

        while True:
            page_count += 1
            logger.debug(f"Fetching page {page_count} for {action_desc}...")
            try:
                # --- Check token validity before API call ---
                # This might be slightly redundant if library handles it, but adds explicit check
                if not self.refresh_token_if_needed():
                     logger.error("Token refresh failed before fetching page. Stopping fetch.")
                     break # Stop fetching if token cannot be maintained

                request_args = {'pageSize': self.batch_size, 'pageToken': nextPageToken} if not album_id else {}
                if album_id:
                     current_body = body.copy()
                     if nextPageToken: current_body['pageToken'] = nextPageToken
                     results = method(body=current_body).execute()
                else:
                     results = method(**request_args).execute()

                items = results.get('mediaItems')

                if not items:
                    logger.info(f"No more items found on page {page_count} for {action_desc}.")
                    break

                all_items.extend(items)
                num_items_in_batch = len(items)
                total_items_retrieved += num_items_in_batch
                logger.info(f"Retrieved {num_items_in_batch} items in batch {page_count}. Total retrieved so far: {total_items_retrieved}")

                nextPageToken = results.get('nextPageToken')
                if not nextPageToken:
                    logger.info(f"No more pages found for {action_desc}. Finished fetching.")
                    break

            except HttpError as error:
                logger.error(f'An HTTP error occurred while retrieving photos from Google Photos {action_desc} (Page {page_count}): {error}')
                if error.resp.status == 401:
                    logger.error("Received 401 Unauthorized error during fetch. Token may be invalid/expired.")
                    # Try one immediate refresh, then break if it fails
                    if not self.refresh_token_if_needed(buffer_minutes=0):
                         logger.error("Immediate token refresh failed. Stopping fetch.")
                         break
                    else:
                         logger.info("Token refreshed after 401, will retry fetch on next iteration if applicable (though loop breaks here).")
                         # Ideally, we'd *retry* the current page fetch here, but breaking is simpler for now.
                         break
                else:
                    # Break on other HttpErrors too
                    break
            except Exception as e:
                logger.error(f'An unexpected error occurred while retrieving photos from Google Photos {action_desc} (Page {page_count}): {e}', exc_info=True)
                break

        logger.info(f"Finished retrieving items from {action_desc}. Total items found: {len(all_items)}")
        return all_items

    def download_photo(self, item):
        """
        Downloads a photo/video item to the temporary directory with retries.
        Returns tuple: (temp_file_path, filename, width, height, mime_type, last_status_code)
        last_status_code is None on success, or the HTTP status code on the last failed attempt.
        """
        # --- No changes needed here from previous version (v1.5) ---
        # Retry logic is already present
        if not self.is_authenticated():
            logger.error("Not authenticated with Google Photos. Cannot download.")
            return None, None, None, None, None, None
        if not self.temp_dir:
            logger.error("Temporary directory not initialized. Cannot download.")
            return None, None, None, None, None, None

        media_item_id = item['id']
        filename = item.get('filename', f"unknown_{media_item_id}")
        mime_type = item.get('mimeType', 'application/octet-stream')
        base_url = item.get('baseUrl')
        is_video = mime_type.startswith('video/')

        if not base_url:
            logger.error(f"Cannot download '{filename}' ({media_item_id}): Missing baseUrl.")
            logger.debug(f"Item data with missing baseUrl: {item}")
            return None, None, None, None, None, None

        if ("/profile/picture/" in base_url or "/gp/p/" in base_url) and not base_url.startswith("https://lh3.googleusercontent.com/lr/AAJ1LKda-hlpwGOSrU6aqBtWOMleYCZJ6nZhwKsjX1z-Z0ES6dN_KQacbIDY0dG2mc9qi3UgodfavTW_xDZGpa2LHBeSQe3SVaSoGZfamxVFyy-zi4hm3ih86BkT-iVvesLX0S0J9s5BlWjEAG9meWm2PRoEyU_5HsLR8ULNa8ijX4c5SUfqqMHNWkRFxsaqQiW3V4CRrnqC1XzBbM-6TU2f9jWGyXTNqqCgNbI7s7kmJ_XkJszidgRuRKO65p74P2pCKN5P5eGpFQuTZC1KTHei9ee3Xhb2zGIu1O9uUGGJGe9XREEkHzZopXkwpY4YVHRBMbdoo81kIrZBuY7GbvDySS5QkffT8tyhatm_WRsPRn0mwRd3tZyZ1kb_QciqD-9bxJrSxVSe0poY6uPEfox4d8J0W1_EqLfiVimI7vqk2wDEDgmQUD0CikXUyGG6vKYVVhvnkPp4HRs3rWNsQQWXG_F08anwIcc3bnKttxapKlsvUJMOtM8cY8pbIb7ufmSmsT6bb8iuljPPdNhag9TMinYbKX7Nxn3nMeW1r3iw6JEZIYxXQUk1RL_W3_Kmn67puY-jGPOVqFnBsEj0_Nm9y3tfr2ghEUDVddEhrjKkec0hNIuz73MZ0akuRVVVh4FpHFbFqP_y62prn1XxLqx4SE7r6W5x_aJixf8DIH0DJQn5dHmprttV9NlRJztg3EEok6J3LcZNG_p2LM1N1aSGirQZ7lwTApKgEGYvUTNY0EvBQA16TevH_PMT9jXA7JQH8_f98KOyYXIBLMn7KrwUTO2j5lfwUusN5-tdcsSXQ_9zAGqY_7ZzfbpqVILMdeDZuX1d3DXdiRTrowAuc8j-Ugj7F8UYgML6pPzJbP_rxC17uu0NyhGlV7_i1Dm9gBzNwsRZUH1Z0CR8H7iteLrJNjoCphmtet4i19Lkb-qrtlTZCnzfquPVWXPwd3-QHCFDV24kSxy6NB-T4SI2j3mu2dVB8XuBLB-gmncZelTAww0fWhXotwX_CjoS5QwD_duOXHOAi9NNosudVwZFBFcai8DLiOvvFlu1=d=d"):
             logger.warning(f"Download skipped for '{filename}' (ID: {media_item_id}) due to potentially invalid baseUrl pattern: {base_url}")
             return None, None, None, None, None, 403

        download_param = "=dv" if is_video else "=d"
        download_url = base_url + download_param

        temp_file_path = None
        try:
            fd, temp_file_path = tempfile.mkstemp(suffix=f"_{filename}", dir=self.temp_dir)
            os.close(fd)
        except Exception as e:
            logger.error(f"Failed to create temporary file for download in {self.temp_dir}: {e}")
            return None, None, None, None, None, None

        logger.debug(f"Attempting to download '{filename}' from {download_url} to {temp_file_path}")

        max_retries = 3
        retry_delay_seconds = 5
        last_status_code = None

        for attempt in range(max_retries):
            try:
                response = requests.get(download_url, stream=True, timeout=120)
                last_status_code = response.status_code
                response.raise_for_status()

                with open(temp_file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192 * 2):
                        f.write(chunk)
                logger.debug(f"Successfully downloaded '{filename}' on attempt {attempt + 1}")
                width = item.get('mediaMetadata', {}).get('width', 0)
                height = item.get('mediaMetadata', {}).get('height', 0)
                return temp_file_path, filename, width, height, mime_type, None

            except requests.exceptions.HTTPError as http_err:
                should_retry = http_err.response.status_code in [401, 403, 429, 500, 503]
                log_level = logging.WARNING if should_retry and attempt < max_retries - 1 else logging.ERROR
                logger.log(log_level, f"HTTP Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {http_err}")
                if hasattr(http_err, 'response') and hasattr(http_err.response, 'text'):
                    if log_level == logging.ERROR or logger.isEnabledFor(logging.DEBUG):
                        logger.log(log_level, f"  Response Text (first 500 chars): {http_err.response.text[:500]}...")
                if should_retry and attempt < max_retries - 1:
                    logger.info(f"Retrying download for '{filename}' in {retry_delay_seconds} seconds... (Status: {last_status_code})")
                    time.sleep(retry_delay_seconds)
                    continue
                else:
                    if http_err.response.status_code in [401, 403]: logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to HTTPError {last_status_code}. URL may have expired.")
                    else: logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to HTTPError {last_status_code}.")
                    break
            except requests.exceptions.RequestException as req_err:
                log_level = logging.WARNING if attempt < max_retries - 1 else logging.ERROR
                logger.log(log_level, f"Request Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {req_err}")
                last_status_code = None
                if attempt < max_retries - 1:
                     logger.info(f"Retrying download for '{filename}' in {retry_delay_seconds} seconds...")
                     time.sleep(retry_delay_seconds)
                     continue
                else:
                     logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to RequestException.")
                     break
            except IOError as io_err:
                logger.error(f"IO Error writing downloaded file '{filename}' to {temp_file_path}: {io_err}")
                last_status_code = None; break
            except Exception as e:
                 logger.error(f"Unexpected Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {e}", exc_info=True)
                 last_status_code = None; break

        logger.error(f"Failed to download '{filename}' (ID: {media_item_id}) after {max_retries} attempts. Last status code: {last_status_code}")
        if temp_file_path and os.path.exists(temp_file_path):
            try: os.remove(temp_file_path); logger.debug(f"Removed potentially failed download file: {temp_file_path}")
            except OSError as e: logger.warning(f"Could not remove temp file {temp_file_path} after failed download attempts: {e}")
        return None, None, None, None, None, last_status_code

    def get_media_item(self, media_item_id):
        """Retrieves details for a single media item by its ID."""
        # --- Add token refresh check before API call ---
        if not self.refresh_token_if_needed():
             logger.error(f"Cannot get media item {media_item_id}: Token refresh failed.")
             return None
        if not self.is_authenticated(): # Check again after potential refresh
             logger.error(f"Not authenticated. Cannot get media item {media_item_id}.")
             return None

        try:
            logger.debug(f"Getting media item details for ID: {media_item_id}")
            item = self.service.mediaItems().get(mediaItemId=media_item_id).execute()
            logger.debug(f"Successfully retrieved details for media item {media_item_id}")
            return item
        except HttpError as error:
            logger.error(f"Failed to get media item {media_item_id}: {error}")
            if error.resp.status == 401:
                logger.error("Received 401 Unauthorized error. Token may be invalid/expired. Re-authentication might be required.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting media item {media_item_id}: {e}", exc_info=True)
            return None

    def remove_photo(self, media_item_id, dry_run=False):
        """Simulates removing a photo from Google Photos by its ID."""
        if not self.is_authenticated():
             logger.error(f"Not authenticated. Cannot {'simulate removing' if dry_run else 'remove'} {media_item_id}.")
             return False
        logger.warning("Google Photos API currently does not support direct deletion of media items.")
        log_prefix = "[DRY RUN] Would remove" if dry_run else "[API UNSUPPORTED] Would remove"
        logger.info(f"{log_prefix} media item {media_item_id} from Google Photos if the API supported it.")
        return True

    def __del__(self):
        """Clean up the temporary directory when the object is destroyed."""
        temp_dir_path = getattr(self, 'temp_dir', None)
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                shutil.rmtree(temp_dir_path)
                print(f"[INFO] Cleaned up temporary directory: {temp_dir_path}")
            except Exception as e:
                print(f"[WARNING] Could not remove temporary directory {temp_dir_path}: {e}")
        elif temp_dir_path:
             print(f"[DEBUG] Temporary directory {temp_dir_path} did not exist or was already removed.")
        else:
             print("[DEBUG] Temporary directory attribute was not set or was None.")