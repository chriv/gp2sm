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
             self.temp_dir = None # Ensure it's None if creation fails

        try:
            self.authenticate()
        except GoogleCredentialsNotFoundError:
            raise # Re-raise to be handled by main
        except Exception as e:
             logger.error(f"An unexpected error occurred during Google Photos authentication: {e}", exc_info=True)
             # Consider if script should exit here, or if main should handle lack of authentication

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
                creds = None # Ensure creds is None if loading fails

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
                    creds = None # Reset creds on refresh failure
                    # Remove potentially bad token file
                    if os.path.exists(self.token_file):
                        try: os.remove(self.token_file); logger.info(f"Removed potentially invalid token file: {self.token_file}")
                        except OSError as rm_err: logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")

            # Check again if we need to run the flow (covers initial load failure, expiry without refresh token, or refresh failure)
            if not creds or not creds.valid:
                logger.info("No valid Google Photos credentials found, initiating authorization flow...")
                try:
                    if not os.path.exists(self.credentials_file):
                        logger.error(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        logger.error(f"Google Photos credentials file NOT FOUND: {self.credentials_file}")
                        logger.error(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        logger.error("Please download your OAuth 2.0 Client ID credentials JSON file")
                        logger.error("from the Google Cloud Console (for a Desktop application)")
                        logger.error(f"and save it as '{self.credentials_file}' in the same directory as the script.")
                        logger.error("Ensure the Google Photos Library API is enabled for your project.")
                        logger.error("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                        raise GoogleCredentialsNotFoundError(f"Google Photos credentials file missing: {self.credentials_file}")

                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                    logger.info("Authorization flow completed.")
                    # Save the new credentials
                    logger.info(f"Saving new Google Photos token to: {self.token_file}")
                    try:
                        with open(self.token_file, 'w') as token:
                            token.write(creds.to_json())
                    except IOError as e:
                        logger.error(f"Error saving new Google Photos token to {self.token_file}: {e}")
                except GoogleCredentialsNotFoundError:
                    raise # Re-raise immediately
                except Exception as flow_err:
                    logger.error(f"An error occurred during the Google OAuth flow: {flow_err}", exc_info=True)
                    self.creds = None # Ensure creds is None on flow failure
                    return False # Indicate failure

            # Final check after potential flow execution
            if not creds or not creds.valid:
                 logger.error("Failed to obtain valid Google Photos credentials after authorization attempt.")
                 self.creds = None # Ensure creds is None on failure
                 return False # Indicate failure

        # Store the valid credentials object
        self.creds = creds

        # Build the service
        try:
            self.service = build('photoslibrary', 'v1', credentials=self.creds, static_discovery=False)
            logger.info("Successfully built Google Photos API service.")
            # Optional: Verify credentials with a quick, low-impact API call
            logger.debug("Verifying Google Photos API credentials...")
            self.service.mediaItems().list(pageSize=1).execute() # Example verification call
            logger.debug("Google Photos API credentials verified.")
            return True
        except HttpError as error:
            logger.error(f'An HTTP error occurred during Google Photos API service build or verification: {error}')
            # Consider token invalid if service build/verification fails
            self.service = None
            self.creds = None # Invalidate creds
            # Attempt to delete potentially invalid token file
            if os.path.exists(self.token_file):
                 try: os.remove(self.token_file); logger.info(f"Removed potentially invalid token file due to API error: {self.token_file}")
                 except OSError as rm_err: logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")
            return False
        except Exception as e:
            logger.error(f'An unexpected error occurred during Google Photos API service build: {e}', exc_info=True)
            self.service = None
            self.creds = None
            return False

    def refresh_token_if_needed(self, buffer_minutes=10):
        """Checks if the token is close to expiry and refreshes it."""
        if not self.creds:
            logger.debug("Token refresh check skipped: Credentials object is missing.")
            return True

        if not self.creds.valid and self.creds.refresh_token:
            logger.info("Credentials are not valid, attempting immediate refresh...")
            buffer_minutes = -1  # Force refresh attempt now
        elif not self.creds.refresh_token:
            logger.debug("Token refresh check skipped: No refresh token available.")
            return self.creds.valid

        if not self.creds.expiry:
            logger.debug("Token refresh check skipped: No expiry information available.")
            # If expiry is unknown, we can't check buffer, but return current validity
            return self.creds.valid

        # --- FIX STARTS HERE ---
        # Get the expiry time and ensure it's timezone-aware (assume UTC if naive)
        expiry_dt = self.creds.expiry
        if expiry_dt.tzinfo is None:
            # If expiry is naive, assume it's UTC and make it aware
            logger.debug("Credentials expiry time was naive, assuming UTC.")
            expiry_aware_dt = expiry_dt.replace(tzinfo=datetime.timezone.utc)
        else:
            # If expiry is already aware, use it directly
            expiry_aware_dt = expiry_dt

        # Get the current time as timezone-aware UTC
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Calculate the refresh threshold time (also timezone-aware)
        refresh_time = expiry_aware_dt - datetime.timedelta(minutes=buffer_minutes if buffer_minutes >= 0 else 0)
        # --- FIX ENDS HERE ---

        # Now compare the two timezone-aware datetime objects
        if now_utc >= refresh_time or buffer_minutes < 0:
            # (Rest of the refresh logic remains the same as the code you have)
            if buffer_minutes < 0:
                logger.info("Attempting immediate token refresh...")
            else:
                logger.info(f"Google Photos token expires soon (at {expiry_aware_dt}). Refreshing proactively...")

            try:
                # Use the stored Request object if available, else create one
                request = Request()
                self.creds.refresh(request)
                logger.info("Token refreshed successfully.")
                # Save the updated token
                logger.info(f"Saving refreshed Google Photos token to: {self.token_file}")
                try:
                    with open(self.token_file, 'w') as token:
                        token.write(self.creds.to_json())
                    return True  # Refresh successful
                except IOError as e:
                    logger.error(f"Error saving refreshed Google Photos token to {self.token_file}: {e}")
                    return False  # Failed to save refreshed token
            except Exception as e:
                logger.error(f"Error during token refresh: {e}. Re-authentication might be needed later.",
                             exc_info=True)  # Added exc_info
                # Consider token invalid after failed refresh
                self.creds = None  # Invalidate creds object
                # Attempt to delete potentially invalid token file
                if os.path.exists(self.token_file):
                    try:
                        os.remove(self.token_file); logger.info(
                            f"Removed potentially invalid token file after refresh error: {self.token_file}")
                    except OSError as rm_err:
                        logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")
                return False  # Indicate refresh failed
        else:
            # Token is valid and not yet time for proactive refresh
            logger.debug(f"Token refresh check: Token valid until {expiry_aware_dt}. No refresh needed yet.")
            return True  # No refresh needed, token is valid

    def get_photos(self, album_id=None):
        """Retrieves a list of photos from the Google Photos library, optionally from a specific album."""
        # --- Attempt refresh/auth if needed before starting ---
        if not self.refresh_token_if_needed(): # Check token status first
             if not self.authenticate(): # If refresh failed or token was bad, try full auth
                  logger.error("Authentication failed before starting photo retrieval. Cannot proceed.")
                  return [] # Return empty list if cannot authenticate

        if not self.is_authenticated(): # Final check
            logger.error("Not authenticated with Google Photos. Cannot retrieve photos.")
            return [] # Return empty list

        all_items = []
        nextPageToken = None
        page_count = 0
        total_items_retrieved = 0

        # Construct the body of the request only if searching within an album
        body = {'albumId': album_id, 'pageSize': self.batch_size} if album_id else {}

        # Choose the correct API method based on whether an album_id is provided
        method_name = 'search' if album_id else 'list'
        method = getattr(self.service.mediaItems(), method_name)

        action_desc = f"album ID: {album_id}" if album_id else "library"
        logger.info(f"Starting to fetch media items from Google Photos {action_desc}...")

        while True:
            page_count += 1
            logger.debug(f"Fetching page {page_count} for {action_desc}...")
            try:
                # --- Proactive token check before each page call ---
                if not self.refresh_token_if_needed():
                     logger.error("Token refresh failed before fetching page. Stopping fetch.")
                     break # Stop fetching if token cannot be maintained

                # Construct request arguments or body based on method
                if album_id:
                     current_body = body.copy()
                     if nextPageToken: current_body['pageToken'] = nextPageToken
                     results = method(body=current_body).execute()
                else:
                     request_args = {'pageSize': self.batch_size, 'pageToken': nextPageToken}
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
                    # Try one immediate refresh; if it fails, break the loop.
                    if not self.refresh_token_if_needed(buffer_minutes=-1): # Force immediate refresh attempt
                         logger.error("Immediate token refresh failed after 401. Stopping fetch.")
                         break
                    else:
                         logger.info("Token refreshed after 401, retrying fetch for page {page_count}...")
                         # Continue loop to retry the *same* page with the refreshed token
                         continue
                elif error.resp.status == 403:
                     logger.error(f"Received 403 Forbidden error. Check API permissions or quota for Google Photos Library API.")
                     break # Typically not recoverable by retry/refresh
                else:
                    # Break on other HttpErrors too
                    logger.error(f"Unhandled HTTP Error ({error.resp.status}). Stopping fetch.")
                    break
            except Exception as e:
                logger.error(f'An unexpected error occurred while retrieving photos from Google Photos {action_desc} (Page {page_count}): {e}', exc_info=True)
                break # Stop on unexpected errors

        logger.info(f"Finished retrieving items from {action_desc}. Total items found: {len(all_items)}")
        return all_items

    def download_photo(self, item):
        """
        Downloads a photo/video item to the temporary directory with retries.
        Returns tuple: (temp_file_path, filename, width, height, mime_type, last_status_code)
        last_status_code is None on success, or the HTTP status code on the last failed attempt.
        """
        # No changes needed here from previous version (v1.5)
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

        # --- Added improved baseUrl check ---
        if not base_url.startswith(("https://lh3.googleusercontent.com/", "https://video.googleusercontent.com/")):
             logger.warning(f"Download potentially skipped for '{filename}' (ID: {media_item_id}) due to unexpected baseUrl pattern: {base_url}")
             # Optionally, you could return a specific code or None,None,... depending on desired behavior
             # For now, let's try it anyway, but log the warning.

        download_param = "=dv" if is_video else "=d"
        download_url = base_url + download_param

        temp_file_path = None
        try:
            # Create a unique filename within the temp directory
            # Use os.path.join for cross-platform compatibility
            safe_filename = "".join(c for c in filename if c.isalnum() or c in ('.', '_', '-')).rstrip()
            if not safe_filename: safe_filename = f"item_{media_item_id}"
            # No need for mkstemp if we just need a path
            temp_file_path = os.path.join(self.temp_dir, f"dl_{int(time.time()*1000)}_{safe_filename}")
            # fd, temp_file_path = tempfile.mkstemp(suffix=f"_{safe_filename}", dir=self.temp_dir)
            # os.close(fd) # Close immediately as we open with 'wb' later
        except Exception as e:
            logger.error(f"Failed to generate temporary file path in {self.temp_dir}: {e}")
            return None, None, None, None, None, None

        logger.debug(f"Attempting to download '{filename}' from {download_url} to {temp_file_path}")

        max_retries = 3
        retry_delay_seconds = 5
        last_status_code = None

        for attempt in range(max_retries):
            try:
                # Use a session object for potential connection reuse
                with requests.Session() as s:
                     # Note: Google download URLs often don't require auth headers once generated
                     response = s.get(download_url, stream=True, timeout=120) # Increased timeout
                     last_status_code = response.status_code
                     response.raise_for_status() # Raises HTTPError for 4xx/5xx

                     with open(temp_file_path, 'wb') as f:
                         # Use shutil.copyfileobj for potentially better performance
                         shutil.copyfileobj(response.raw, f, length=16*1024)
                         # for chunk in response.iter_content(chunk_size=16384): # 16KB chunk size
                         #    f.write(chunk)

                logger.debug(f"Successfully downloaded '{filename}' on attempt {attempt + 1}")
                # Extract metadata safely
                metadata = item.get('mediaMetadata', {})
                width = metadata.get('width', 0)
                height = metadata.get('height', 0)
                # Convert width/height to int if they are strings (API might return strings)
                try: width = int(width)
                except (ValueError, TypeError): width = 0
                try: height = int(height)
                except (ValueError, TypeError): height = 0

                return temp_file_path, filename, width, height, mime_type, None # Success

            except requests.exceptions.HTTPError as http_err:
                # Decide if retry makes sense based on status code
                should_retry = http_err.response.status_code in [401, 403, 429, 500, 503]
                log_level = logging.WARNING if should_retry and attempt < max_retries - 1 else logging.ERROR
                logger.log(log_level, f"HTTP Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {http_err}")
                # Log response text only on final error or if debug enabled
                if log_level == logging.ERROR or logger.isEnabledFor(logging.DEBUG):
                    if hasattr(http_err, 'response') and http_err.response is not None:
                        try:
                             logger.log(log_level, f"  Response Text (first 500 chars): {http_err.response.text[:500]}...")
                        except Exception: # Handle cases where response.text might not be available/readable
                             logger.log(log_level, "  Response Text could not be read.")

                if should_retry and attempt < max_retries - 1:
                    logger.info(f"Retrying download for '{filename}' in {retry_delay_seconds} seconds... (Status: {last_status_code})")
                    time.sleep(retry_delay_seconds)
                    continue # Go to next attempt
                else:
                    # Final attempt failed or non-retriable error
                    if last_status_code in [401, 403]:
                        logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to HTTPError {last_status_code}. URL may have expired or requires re-fetch.")
                    else:
                        logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to HTTPError {last_status_code}.")
                    break # Exit retry loop

            except requests.exceptions.RequestException as req_err:
                # Includes connection errors, timeouts etc. Generally worth retrying.
                log_level = logging.WARNING if attempt < max_retries - 1 else logging.ERROR
                logger.log(log_level, f"Request Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {req_err}")
                last_status_code = None # No HTTP status code for these errors
                if attempt < max_retries - 1:
                     logger.info(f"Retrying download for '{filename}' in {retry_delay_seconds} seconds...")
                     time.sleep(retry_delay_seconds)
                     continue # Go to next attempt
                else:
                     logger.error(f"Download failed for '{filename}' after {attempt + 1} attempts due to RequestException.")
                     break # Exit retry loop

            except IOError as io_err:
                 logger.error(f"IO Error writing downloaded file '{filename}' to {temp_file_path}: {io_err}")
                 last_status_code = None # Not an HTTP error
                 break # Exit retry loop
            except Exception as e:
                 logger.error(f"Unexpected Error downloading '{filename}' on attempt {attempt + 1}/{max_retries}: {e}", exc_info=True)
                 last_status_code = None # Not an HTTP error
                 break # Exit retry loop

        # If loop finished without returning success
        logger.error(f"Failed to download '{filename}' (ID: {media_item_id}) after {max_retries} attempts. Last status code: {last_status_code}")
        # Clean up the potentially partially downloaded or empty temp file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Removed potentially failed download file: {temp_file_path}")
            except OSError as e:
                logger.warning(f"Could not remove temp file {temp_file_path} after failed download attempts: {e}")
        return None, None, None, None, None, last_status_code # Return failure indication

    def get_media_item(self, media_item_id):
        """Retrieves details for a single media item by its ID."""
        # --- Attempt refresh/auth if needed before API call ---
        if not self.refresh_token_if_needed():
             if not self.authenticate():
                  logger.error(f"Authentication failed before getting media item {media_item_id}. Cannot proceed.")
                  return None

        if not self.is_authenticated(): # Final check
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
                logger.error("Received 401 Unauthorized error. Token may be invalid/expired.")
                # Attempt immediate refresh; if it fails, caller might need to re-authenticate fully
                self.refresh_token_if_needed(buffer_minutes=-1) # Force refresh attempt
            elif error.resp.status == 404:
                 logger.error(f"Received 404 Not Found error for media item {media_item_id}. It might have been deleted.")
            # Allow caller to handle the None return value
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting media item {media_item_id}: {e}", exc_info=True)
            return None

    def remove_photo(self, media_item_id, dry_run=False):
        """Simulates removing a photo from Google Photos by its ID."""
        # Google Photos API does not support deleting items via API as of last check.
        # This function only logs the action.
        if not self.is_authenticated():
             logger.error(f"Not authenticated. Cannot {'simulate removing' if dry_run else 'remove'} {media_item_id}.")
             return False
        logger.warning("Google Photos API currently does not support direct deletion of media items.")
        log_prefix = "[DRY RUN] Would remove" if dry_run else "[API UNSUPPORTED] Would remove"
        logger.info(f"{log_prefix} media item {media_item_id} from Google Photos if the API supported it.")
        # Always return True because the *simulated* action is considered successful for workflow.
        return True

    # --- NEW Method ---
    def cleanup_temp_dir(self):
        """Explicitly cleans up the temporary directory."""
        temp_dir_path = getattr(self, 'temp_dir', None)
        logger.debug(f"Explicit cleanup called for temp dir: {temp_dir_path}")
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                shutil.rmtree(temp_dir_path)
                # Use logger instead of print for consistency
                logger.info(f"Successfully removed temporary directory: {temp_dir_path}")
                self.temp_dir = None # Mark as removed
            except Exception as e:
                # Log the error properly
                logger.error(f"Error removing temporary directory {temp_dir_path}: {e}", exc_info=True)
                # Optionally print a console message if desired for high visibility
                # print(f"[ERROR] Failed to remove temporary directory {temp_dir_path}: {e}")
        elif temp_dir_path:
             logger.debug(f"Temporary directory {temp_dir_path} did not exist or was already removed.")
        else:
             logger.debug("Temporary directory attribute was not set or was None during explicit cleanup.")
