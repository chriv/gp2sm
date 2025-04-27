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
            # Create temporary directory in system temp location
            self.temp_dir = tempfile.mkdtemp(prefix="gp2sm_")
            logger.debug(f"Created temporary directory: {self.temp_dir}")
        except Exception as e:
             logger.error(f"Failed to create temporary directory: {e}", exc_info=True)
             self.temp_dir = None # Ensure it's None if creation fails

        try:
            # Authenticate on initialization
            self.authenticate()
        except GoogleCredentialsNotFoundError:
            raise # Re-raise to be handled by main
        except Exception as e:
             logger.error(f"An unexpected error occurred during Google Photos initialization: {e}", exc_info=True)
             # Let main handle the failure if authentication doesn't succeed

    def is_authenticated(self):
        """Returns True if authenticated with Google Photos, False otherwise."""
        return self.service is not None and self.creds and self.creds.valid

    def authenticate(self):
        """
        Authenticates with the Google Photos API using OAuth 2.0.
        Stores credentials in self.creds. Returns True on success, False otherwise.
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
                    try:
                        with open(self.token_file, 'w') as token: token.write(creds.to_json())
                        logger.debug(f"Saved refreshed Google Photos token to: {self.token_file}")
                    except IOError as e: logger.error(f"Error saving refreshed token to {self.token_file}: {e}")
                except Exception as e:
                    logger.error(f"Error refreshing Google Photos token: {e}. User may need to re-authorize.")
                    creds = None
                    if os.path.exists(self.token_file):
                        try: os.remove(self.token_file); logger.info(f"Removed invalid token file: {self.token_file}")
                        except OSError as rm_err: logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")

            if not creds or not creds.valid:
                logger.info("No valid Google Photos credentials found, initiating authorization flow...")
                try:
                    if not os.path.exists(self.credentials_file):
                        logger.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        logger.error(f"Google Photos credentials file NOT FOUND: {self.credentials_file}")
                        logger.error("Please download OAuth 2.0 Client ID (Desktop app) JSON from Google Cloud Console")
                        logger.error(f"and save it as '{self.credentials_file}' in the script directory.")
                        logger.error("Ensure the Google Photos Library API is enabled.")
                        logger.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        raise GoogleCredentialsNotFoundError(f"Missing: {self.credentials_file}")

                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                    logger.info("Authorization flow completed.")
                    try:
                        with open(self.token_file, 'w') as token: token.write(creds.to_json())
                        logger.info(f"Saved new Google Photos token to: {self.token_file}")
                    except IOError as e: logger.error(f"Error saving new token to {self.token_file}: {e}")
                except GoogleCredentialsNotFoundError: raise
                except Exception as flow_err:
                    logger.error(f"An error occurred during the Google OAuth flow: {flow_err}", exc_info=True)
                    self.creds = None; return False

            if not creds or not creds.valid:
                 logger.error("Failed to obtain valid Google Photos credentials after authorization attempt.")
                 self.creds = None; return False

        self.creds = creds
        try:
            self.service = build('photoslibrary', 'v1', credentials=self.creds, static_discovery=False)
            logger.info("Successfully built Google Photos API service.")
            # Optional: Verify credentials
            logger.debug("Verifying Google Photos API credentials...")
            self.service.mediaItems().list(pageSize=1).execute()
            logger.debug("Google Photos API credentials verified.")
            return True
        except HttpError as error:
            logger.error(f'An HTTP error occurred during service build/verification: {error}')
            self.service = None; self.creds = None
            if os.path.exists(self.token_file):
                 try: os.remove(self.token_file); logger.info(f"Removed potentially invalid token file due to API error: {self.token_file}")
                 except OSError as rm_err: logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")
            return False
        except Exception as e:
            logger.error(f'An unexpected error during service build: {e}', exc_info=True)
            self.service = None; self.creds = None
            return False

    def refresh_token_if_needed(self, buffer_minutes=10):
        """Checks if the token is close to expiry and refreshes it."""
        if not self.creds: return True # Nothing to do

        if not self.creds.valid and self.creds.refresh_token:
             logger.info("Credentials are not valid, attempting immediate refresh...")
             buffer_minutes = -1 # Force refresh attempt
        elif not self.creds.refresh_token:
             logger.debug("Token refresh check skipped: No refresh token.")
             return self.creds.valid

        if not self.creds.expiry:
             logger.debug("Token refresh check skipped: No expiry info.")
             return self.creds.valid

        # --- Timezone Fix ---
        expiry_dt = self.creds.expiry
        if expiry_dt.tzinfo is None:
            logger.debug("Credentials expiry time was naive, assuming UTC.")
            expiry_aware_dt = expiry_dt.replace(tzinfo=datetime.timezone.utc)
        else:
            expiry_aware_dt = expiry_dt
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        refresh_time = expiry_aware_dt - datetime.timedelta(minutes=buffer_minutes if buffer_minutes >= 0 else 0)
        # --- End Timezone Fix ---

        if now_utc >= refresh_time or buffer_minutes < 0:
            if buffer_minutes < 0: logger.info("Attempting immediate token refresh...")
            else: logger.info(f"Token expires soon (at {expiry_aware_dt}). Refreshing proactively...")
            try:
                self.creds.refresh(Request())
                logger.info("Token refreshed successfully.")
                try:
                    with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                    logger.debug(f"Saved refreshed Google Photos token to: {self.token_file}")
                    return True
                except IOError as e: logger.error(f"Error saving refreshed token to {self.token_file}: {e}"); return False
            except Exception as e:
                logger.error(f"Error during token refresh: {e}. Re-auth needed later.", exc_info=True)
                self.creds = None
                if os.path.exists(self.token_file):
                     try: os.remove(self.token_file); logger.info(f"Removed token file after refresh error: {self.token_file}")
                     except OSError as rm_err: logger.warning(f"Could not remove token file {self.token_file}: {rm_err}")
                return False
        else:
            logger.debug(f"Token refresh check: Token valid until {expiry_aware_dt}. No refresh needed.")
            return True

    def get_photos(self, album_id=None):
        """Retrieves a list of photos from the library or a specific album."""
        if not self.refresh_token_if_needed():
             if not self.authenticate():
                  logger.error("Auth failed before photo retrieval.")
                  return []
        if not self.is_authenticated():
            logger.error("Not authenticated. Cannot retrieve photos.")
            return []

        all_items = []; nextPageToken = None; page_count = 0; total_items_retrieved = 0
        body = {'albumId': album_id, 'pageSize': self.batch_size} if album_id else {}
        method_name = 'search' if album_id else 'list'
        method = getattr(self.service.mediaItems(), method_name)
        action_desc = f"album ID: {album_id}" if album_id else "library"
        logger.info(f"Starting fetch from Google Photos {action_desc}...")

        while True:
            page_count += 1
            logger.debug(f"Fetching page {page_count} for {action_desc}...")
            try:
                if not self.refresh_token_if_needed():
                     logger.error("Token refresh failed mid-fetch. Stopping."); break

                if album_id:
                     current_body = body.copy()
                     if nextPageToken: current_body['pageToken'] = nextPageToken
                     results = method(body=current_body).execute()
                else:
                     request_args = {'pageSize': self.batch_size, 'pageToken': nextPageToken}
                     results = method(**request_args).execute()

                items = results.get('mediaItems')
                if not items: logger.info(f"No more items found on page {page_count}."); break

                all_items.extend(items); num_items_in_batch = len(items); total_items_retrieved += num_items_in_batch
                logger.info(f"Retrieved {num_items_in_batch} items in batch {page_count}. Total: {total_items_retrieved}")

                nextPageToken = results.get('nextPageToken')
                if not nextPageToken: logger.info(f"No more pages. Finished fetching."); break

            except HttpError as error:
                logger.error(f'HTTP error retrieving photos from {action_desc} (Page {page_count}): {error}')
                if error.resp.status == 401:
                    logger.error("401 Unauthorized. Trying immediate refresh...")
                    if not self.refresh_token_if_needed(buffer_minutes=-1):
                         logger.error("Immediate refresh failed. Stopping fetch."); break
                    else: logger.info("Refreshed after 401, retrying page..."); continue
                elif error.resp.status == 403: logger.error(f"403 Forbidden. Check API permissions/quota."); break
                else: logger.error(f"Unhandled HTTP Error ({error.resp.status}). Stopping fetch."); break
            except Exception as e: logger.error(f'Unexpected error retrieving photos (Page {page_count}): {e}', exc_info=True); break

        logger.info(f"Finished fetching from {action_desc}. Total items: {len(all_items)}")
        return all_items

    def download_photo(self, item):
        """
        Downloads a photo/video item. Fail fast on 401/403 errors.
        Returns tuple: (temp_file_path, filename, width, height, mime_type, last_status_code)
        """
        if not self.is_authenticated(): logger.error("Not authenticated."); return None, None, None, None, None, None
        if not self.temp_dir: logger.error("Temp directory not initialized."); return None, None, None, None, None, None
        if not isinstance(item, dict): logger.error(f"Invalid item data type: {type(item)}"); return None, None, None, None, None, None

        media_item_id = item.get('id', 'UnknownID'); filename = item.get('filename', f"unknown_{media_item_id}")
        mime_type = item.get('mimeType', 'application/octet-stream'); base_url = item.get('baseUrl')
        is_video = mime_type.startswith('video/')

        if not base_url: logger.error(f"Missing baseUrl for '{filename}' ({media_item_id})"); return None, None, None, None, None, None

        download_param = "=dv" if is_video else "=d"; download_url = base_url + download_param
        temp_file_path = None
        try:
            safe_filename = "".join(c for c in filename if c.isalnum() or c in ('.', '_', '-')).rstrip()
            if not safe_filename: safe_filename = f"item_{media_item_id}"
            temp_file_path = os.path.join(self.temp_dir, f"dl_{int(time.time()*1000)}_{safe_filename}")
        except Exception as e: logger.error(f"Failed to create temp file path: {e}"); return None, None, None, None, None, None

        logger.debug(f"Attempting to download '{filename}' to {temp_file_path}")

        max_retries = 3; retry_delay_seconds = 5; last_status_code = None

        for attempt in range(max_retries):
            try:
                with requests.Session() as s:
                     response = s.get(download_url, stream=True, timeout=120)
                     last_status_code = response.status_code
                     response.raise_for_status()
                     with open(temp_file_path, 'wb') as f: shutil.copyfileobj(response.raw, f, length=16*1024)

                logger.debug(f"Successfully downloaded '{filename}' on attempt {attempt + 1}")
                metadata = item.get('mediaMetadata', {}); width = metadata.get('width', 0); height = metadata.get('height', 0)
                try: width = int(width)
                except (ValueError, TypeError): width = 0
                try: height = int(height)
                except (ValueError, TypeError): height = 0
                return temp_file_path, filename, width, height, mime_type, None # Success

            except requests.exceptions.HTTPError as http_err:
                last_status_code = http_err.response.status_code if http_err.response else None
                log_level = logging.ERROR # Default to error for HTTP issues

                # --- Fail Fast Logic ---
                if last_status_code in [401, 403]:
                    logger.log(log_level, f"HTTP Error downloading '{filename}' on attempt {attempt + 1}: {http_err} (Status: {last_status_code}). URL likely invalid/expired.")
                    # Log response text only if debug enabled
                    if logger.isEnabledFor(logging.DEBUG):
                         if hasattr(http_err, 'response') and http_err.response is not None:
                              try: logger.debug(f"  Response Text (first 500 chars): {http_err.response.text[:500]}...")
                              except Exception: logger.debug("  Response Text could not be read.")
                    break # Exit retry loop immediately for 401/403
                # --- End Fail Fast Logic ---

                # Decide if retry makes sense for *other* status codes
                should_retry_other = last_status_code in [429, 500, 503]
                log_level = logging.WARNING if should_retry_other and attempt < max_retries - 1 else logging.ERROR
                logger.log(log_level, f"HTTP Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {http_err}")

                if log_level == logging.ERROR or logger.isEnabledFor(logging.DEBUG):
                     if hasattr(http_err, 'response') and http_err.response is not None:
                          try: logger.log(log_level, f"  Response Text (first 500 chars): {http_err.response.text[:500]}...")
                          except Exception: logger.log(log_level, "  Response Text could not be read.")

                if should_retry_other and attempt < max_retries - 1:
                    logger.info(f"Retrying download for '{filename}' in {retry_delay_seconds} seconds... (Status: {last_status_code})")
                    time.sleep(retry_delay_seconds)
                    continue
                else:
                    logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to HTTPError {last_status_code}.")
                    break

            except requests.exceptions.RequestException as req_err:
                log_level = logging.WARNING if attempt < max_retries - 1 else logging.ERROR
                logger.log(log_level, f"Request Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {req_err}")
                last_status_code = None
                if attempt < max_retries - 1:
                     logger.info(f"Retrying download for '{filename}' in {retry_delay_seconds} seconds...")
                     time.sleep(retry_delay_seconds); continue
                else: logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to RequestException."); break

            except IOError as io_err: logger.error(f"IO Error writing file '{filename}': {io_err}"); last_status_code = None; break
            except Exception as e: logger.error(f"Unexpected Error downloading '{filename}': {e}", exc_info=True); last_status_code = None; break

        # After loop
        # Log final failure only if it wasn't a 401/403 (which logged their error inside the loop)
        if temp_file_path is None and last_status_code not in [401, 403]:
             logger.error(f"Failed to download '{filename}' (ID: {media_item_id}) after retries. Last status code: {last_status_code}")

        if temp_file_path and os.path.exists(temp_file_path) and last_status_code is not None : # If download failed, cleanup temp file
            try: os.remove(temp_file_path); logger.debug(f"Removed failed download file: {temp_file_path}")
            except OSError as e: logger.warning(f"Could not remove temp file {temp_file_path} after failed download: {e}")
            temp_file_path = None # Ensure path is None on failure

        # Return None for path on failure, along with the error code
        if temp_file_path is None:
             return None, None, None, None, None, last_status_code
        else:
             # Should not be reached if loop exited due to failure, but safeguard
             return temp_file_path, filename, width, height, mime_type, None


    def get_media_item(self, media_item_id):
        """Retrieves details for a single media item by its ID."""
        if not self.refresh_token_if_needed():
             if not self.authenticate(): logger.error(f"Auth failed before getting item {media_item_id}."); return None
        if not self.is_authenticated(): logger.error(f"Not authenticated. Cannot get item {media_item_id}."); return None

        try:
            logger.debug(f"Getting media item details for ID: {media_item_id}")
            item = self.service.mediaItems().get(mediaItemId=media_item_id).execute()
            logger.debug(f"Successfully retrieved details for media item {media_item_id}")
            return item
        except HttpError as error:
            logger.error(f"Failed to get media item {media_item_id}: {error}")
            if error.resp.status == 401: logger.error("Received 401. Token may be invalid."); self.refresh_token_if_needed(buffer_minutes=-1)
            elif error.resp.status == 404: logger.error(f"Received 404 Not Found for {media_item_id}.")
            return None
        except Exception as e: logger.error(f"Unexpected error getting media item {media_item_id}: {e}", exc_info=True); return None

    def remove_photo(self, media_item_id, dry_run=False):
        """Simulates removing a photo from Google Photos by its ID."""
        if not self.is_authenticated(): logger.error(f"Not auth. Cannot {'simulate remove' if dry_run else 'remove'} {media_item_id}."); return False
        logger.warning("Google Photos API currently does not support direct deletion.")
        log_prefix = "[DRY RUN] Would remove" if dry_run else "[API UNSUPPORTED] Would remove"
        logger.info(f"{log_prefix} media item {media_item_id} from Google Photos.")
        return True # Simulated action is always 'successful'

    # Explicit cleanup method (replaces __del__)
    def cleanup_temp_dir(self):
        """Explicitly cleans up the temporary directory."""
        temp_dir_path = getattr(self, 'temp_dir', None)
        logger.debug(f"Explicit cleanup called for temp dir: {temp_dir_path}")
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                shutil.rmtree(temp_dir_path)
                logger.info(f"Successfully removed temporary directory: {temp_dir_path}")
                self.temp_dir = None # Mark as removed
            except Exception as e:
                logger.error(f"Error removing temporary directory {temp_dir_path}: {e}", exc_info=True)
                # Optionally print a console message if desired for high visibility
                # print(f"[ERROR] Failed to remove temporary directory {temp_dir_path}: {e}")
        elif temp_dir_path:
             logger.debug(f"Temporary directory {temp_dir_path} did not exist or was already removed.")
        else:
             logger.debug("Temporary directory attribute was not set or was None during explicit cleanup.")
