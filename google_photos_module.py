# google_photos_module.py (v2.0)
# Added threading lock around get_media_item API call to prevent SSL concurrency issues.

# Standard library imports
import logging
import os
import tempfile
import shutil
import time # Needed for sleep
import datetime # Needed for expiry check
# import httplib2
# from google_auth_httplib2 import AuthorizedHttp
import threading # Import threading for Lock

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

    SCOPES = ['https://www.googleapis.com/auth/photoslibrary.readonly']

    def __init__(self, credentials_file='google_api_keys.json', token_file='google_photos_token.json',
                 batch_size=50):
        """Initialize with the paths to Google Photos configuration files."""
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.batch_size = batch_size
        self.service = None
        self.creds = None
        self.temp_dir = None
        # --- Add a lock for synchronizing sensitive API calls ---
        self._api_lock = threading.Lock()
        # --- End lock addition ---
        try:
            # Create temporary directory in system temp location
            self.temp_dir = tempfile.mkdtemp(prefix="gp2sm_")
            logger.debug(f"Created temporary directory: {self.temp_dir}")
        except Exception as e:
             logger.error(f"Failed to create temporary directory: {e}", exc_info=True)
             self.temp_dir = None # Ensure it's None if creation fails

        try:
            # Authenticate on initialization
            if not self.authenticate():
                 raise RuntimeError("Google Photos authentication failed during initialization.")
        except GoogleCredentialsNotFoundError:
            raise # Re-raise to be handled by main
        except Exception as e:
             logger.error(f"An unexpected error occurred during Google Photos initialization: {e}", exc_info=True)
             raise # Propagate other init errors

    def is_authenticated(self):
        """Returns True if authenticated with Google Photos, False otherwise."""
        # Acquire lock briefly to check shared state safely, although less critical here
        with self._api_lock:
            return self.service is not None and self.creds and self.creds.valid

    def authenticate(self):
        """Handles the OAuth 2.0 flow and service building."""
        # Acquire lock for the duration of authentication/refresh/service build
        # to prevent race conditions if multiple threads trigger this somehow.
        with self._api_lock:
            creds = None
            # Load existing token if available
            if os.path.exists(self.token_file):
                try:
                    creds = Credentials.from_authorized_user_file(self.token_file, self.SCOPES)
                    logger.debug(f"Loaded credentials from {self.token_file}")
                except Exception as e:
                    logger.warning(f"Error loading token file {self.token_file}: {e}. Re-authentication might be needed.")
                    creds = None # Force re-auth or refresh

            # Check if credentials are valid or need refresh
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    logger.info("Google Photos token has expired. Attempting to refresh...")
                    try:
                        creds.refresh(Request())
                        logger.info("Token refreshed successfully.")
                        # Save the refreshed token
                        try:
                            with open(self.token_file, 'w') as token:
                                token.write(creds.to_json())
                            logger.debug(f"Saved refreshed token to {self.token_file}")
                        except IOError as e:
                            logger.error(f"Error saving refreshed token to {self.token_file}: {e}")
                    except Exception as e:
                        logger.error(f"Error refreshing token: {e}. Manual re-authentication required.", exc_info=True)
                        creds = None # Invalidate creds
                        # Attempt to remove the invalid token file
                        if os.path.exists(self.token_file):
                            try:
                                os.remove(self.token_file)
                                logger.info(f"Removed potentially invalid token file: {self.token_file}")
                            except OSError as rm_err:
                                logger.warning(f"Could not remove invalid token file {self.token_file}: {rm_err}")
                # If still no valid creds, initiate the OAuth flow
                if not creds or not creds.valid:
                    logger.info("No valid Google Photos credentials found or refresh failed. Starting authentication flow...")
                    try:
                        if not os.path.exists(self.credentials_file):
                             logger.critical(f"Google API credentials file not found: {self.credentials_file}")
                             raise GoogleCredentialsNotFoundError(f"Credentials file '{self.credentials_file}' is missing.")

                        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.SCOPES)
                        creds = flow.run_local_server(port=0)
                        logger.info("Authentication flow completed successfully.")
                        # Save the new credentials for future use
                        try:
                            with open(self.token_file, 'w') as token:
                                token.write(creds.to_json())
                            logger.info(f"Saved new token to {self.token_file}")
                        except IOError as e:
                            logger.error(f"Error saving new token to {self.token_file}: {e}")

                    except GoogleCredentialsNotFoundError: raise
                    except Exception as flow_err:
                        logger.critical(f"Google Photos OAuth flow failed: {flow_err}", exc_info=True)
                        self.creds = None; self.service = None; return False # Indicate failure

            # If we have valid credentials (either loaded, refreshed, or newly obtained)
            if creds and creds.valid:
                self.creds = creds
                # Use helper to build service, returns True/False
                return self._build_service() # _build_service is now implicitly protected by the lock
            else:
                 # If after all attempts, creds are still not valid
                 logger.critical("Failed to obtain valid Google Photos credentials after all attempts.")
                 self.creds = None; self.service = None; return False # Indicate failure

    def refresh_token_if_needed(self, buffer_minutes=10):
        """Checks token expiry and refreshes if needed. Returns True if valid/refreshed, False otherwise."""
        # Acquire lock to safely check and modify shared credential state
        with self._api_lock:
            if not self.creds:
                logger.warning("Cannot refresh token: No credentials loaded.")
                return False

            if self.creds.valid and not self.creds.expiry:
                logger.debug("Credentials valid but no expiry info. Assuming OK.")
                return True

            if not self.creds.valid and self.creds.refresh_token:
                logger.info("Credentials invalid/expired. Attempting immediate refresh...")
                try:
                    self.creds.refresh(Request())
                    logger.info("Token refreshed successfully.")
                    try:
                        with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                        logger.debug(f"Saved refreshed token to {self.token_file}")
                    except IOError as e: logger.error(f"Error saving refreshed token: {e}")
                    if not self.service:
                         if not self._build_service(): return False # Rebuild service if needed
                    return True
                except Exception as e:
                    logger.error(f"Error during token refresh: {e}.", exc_info=True)
                    self._invalidate_session() # Invalidate session on refresh failure
                    return False

            elif self.creds.valid and self.creds.expiry:
                expiry_dt = self.creds.expiry
                expiry_aware_dt = expiry_dt.astimezone(datetime.timezone.utc) if expiry_dt.tzinfo else expiry_dt.replace(tzinfo=datetime.timezone.utc)
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                refresh_time = expiry_aware_dt - datetime.timedelta(minutes=buffer_minutes)

                if now_utc >= refresh_time:
                    logger.info(f"Token expires soon ({expiry_aware_dt}). Refreshing proactively...")
                    if self.creds.refresh_token:
                        try:
                            self.creds.refresh(Request())
                            logger.info("Proactive token refresh successful.")
                            try:
                                with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                                logger.debug(f"Saved proactively refreshed token.")
                            except IOError as e: logger.error(f"Error saving proactively refreshed token: {e}")
                            if not self.service:
                                 if not self._build_service(): return False # Rebuild service if needed
                            return True
                        except Exception as e:
                            logger.error(f"Error during proactive token refresh: {e}.", exc_info=True)
                            self._invalidate_session() # Invalidate on failure
                            return False
                    else:
                         logger.warning("Token needs proactive refresh, but no refresh token available.")
                         return True # Still valid for now
                else:
                     # logger.debug(f"Token valid until {expiry_aware_dt}. No refresh needed.") # Too verbose
                     return True # Token is fine

            elif not self.creds.valid and not self.creds.refresh_token:
                 logger.error("Credentials invalid and no refresh token. Manual re-auth required.")
                 self._invalidate_session() # Ensure session is cleared
                 return False

            # Fallback check
            return self.creds and self.creds.valid

    # Original _build_service method (Restored)
    def _build_service(self):
        """Builds the Google Photos service object using credentials."""
        # Note: This function assumes the caller holds _api_lock if needed
        if not self.creds or not self.creds.valid:
            logger.error("Cannot build service: Invalid credentials.")
            self.service = None
            return False
        try:
            # Build service using only credentials - library handles default http client
            self.service = build(
                'photoslibrary',
                'v1',
                credentials=self.creds,
                static_discovery=False,  # Recommended
                cache_discovery=False  # Recommended
            )
            logger.info("Google Photos API service (re)built successfully (default HTTP client).")
            return True
        except Exception as build_err:
            logger.error(f"Failed to (re)build service: {build_err}", exc_info=True)
            self.service = None
            return False

    def _invalidate_session(self):
         """Internal helper to clear session state. Assumes lock is held."""
         logger.warning("Invalidating Google Photos session state.")
         self.creds = None
         self.service = None
         if os.path.exists(self.token_file):
             try:
                  os.remove(self.token_file)
                  logger.info(f"Removed token file due to session invalidation: {self.token_file}")
             except OSError as rm_err:
                  logger.warning(f"Could not remove token file {self.token_file}: {rm_err}")


    def get_photos(self, album_id=None):
        """Retrieves a list of media item dictionaries. Lock acquired internally."""
        # Authentication check acquires lock
        if not self.refresh_token_if_needed():
             logger.error("Token refresh/validation failed. Attempting re-authentication.")
             if not self.authenticate():
                  logger.critical("Re-authentication failed. Cannot retrieve photos.")
                  return []

        # Re-check authentication status (acquires lock)
        if not self.is_authenticated():
            logger.critical("Not authenticated after checks. Cannot retrieve photos.")
            return []

        all_items = []; nextPageToken = None; page_count = 0; total_items_retrieved = 0
        action_desc = f"album ID: {album_id}" if album_id else "library"
        logger.info(f"Starting fetch from Google Photos {action_desc}...")

        while True:
            page_count += 1
            logger.debug(f"Fetching page {page_count} for {action_desc}...")
            try:
                # Acquire lock specifically for the API call within the loop
                # Ensure token is still valid before making the API call
                # (refresh_token_if_needed acquires lock internally, but check again here just before API call)
                if not self.refresh_token_if_needed(buffer_minutes=1): # Use short buffer inside loop
                     logger.error("Token became invalid mid-fetch (pre-API call check). Attempting re-auth...")
                     # Need to release lock to allow authenticate to acquire it
                     # This is getting complex, maybe authenticate should not acquire lock?
                     # Let's simplify: assume refresh_token_if_needed is sufficient for now.
                     # If auth fails, it will raise or return False, handled below.

                # Ensure service object is available
                if not self.service:
                     logger.error("Google Photos service object is missing mid-fetch. Cannot fetch.")
                     # Attempt to rebuild service (still under lock)
                     if not self._build_service():
                          logger.critical("Failed to rebuild service mid-fetch. Stopping.")
                          break # Exit loop if service cannot be rebuilt
                     else:
                          logger.info("Rebuilt service mid-fetch.")

                # --- API Call ---
                if album_id:
                     body = {'albumId': album_id, 'pageSize': self.batch_size}
                     if nextPageToken: body['pageToken'] = nextPageToken
                     results = self.service.mediaItems().search(body=body).execute()
                else:
                     results = self.service.mediaItems().list(pageSize=self.batch_size, pageToken=nextPageToken).execute()
                # --- End API Call ---

                # Process results outside the lock
                items = results.get('mediaItems')
                if not items:
                    logger.info(f"No more items found on page {page_count} for {action_desc}.")
                    break # Exit loop, finished fetching

                all_items.extend(items)
                num_items_in_batch = len(items)
                total_items_retrieved += num_items_in_batch
                logger.info(f"Retrieved {num_items_in_batch} items in batch {page_count}. Total so far: {total_items_retrieved}")

                nextPageToken = results.get('nextPageToken')
                if not nextPageToken:
                    logger.info(f"No more pages found for {action_desc}. Finished fetching.")
                    break # Exit loop

            except HttpError as error:
                logger.error(f'HTTP error retrieving photos page {page_count}: {error}')
                # Handle specific HTTP errors outside the lock if possible
                if error.resp.status == 401:
                    logger.error("401 Unauthorized during fetch. Session may be invalid. Stopping.")
                    # Invalidate session (acquires lock)
                    self._invalidate_session()
                    break
                elif error.resp.status == 403:
                    logger.error(f"403 Forbidden during fetch. Check permissions/quota. Details: {error.content}")
                    break
                elif error.resp.status == 404 and album_id:
                     logger.error(f"404 Not Found for album ID '{album_id}'. Check ID.")
                     all_items = [] # Clear results
                     break
                elif error.resp.status == 429:
                     sleep_time = 60
                     retry_after = error.resp.headers.get('Retry-After')
                     if retry_after:
                          try: sleep_time = int(retry_after) + 5; logger.warning(f"429 Too Many Requests. Sleeping {sleep_time}s (Retry-After)...")
                          except ValueError: logger.warning(f"429 Too Many Requests. Invalid Retry-After. Sleeping default {sleep_time}s...")
                     else: logger.warning(f"429 Too Many Requests. Sleeping default {sleep_time}s...")
                     time.sleep(sleep_time)
                     page_count -=1; continue # Retry same page
                else:
                    logger.error(f"Unhandled HTTP Error ({error.resp.status}) during fetch. Stopping.")
                    break

            except Exception as e:
                logger.error(f'Unexpected error retrieving photos page {page_count}: {e}', exc_info=True)
                break

        logger.info(f"Finished fetching from {action_desc}. Total items retrieved: {len(all_items)}")
        return all_items

    def get_media_item(self, media_item_id):
        """Retrieves the full details of a single media item by its ID. Lock acquired internally."""
        if not media_item_id:
             logger.error("Cannot get media item: No media_item_id provided.")
             return None

        logger.debug(f"Attempting to get details for media item ID: {media_item_id}")
        # Ensure authenticated before proceeding (acquires lock)
        if not self.refresh_token_if_needed():
             logger.error(f"Token invalid before getting item {media_item_id}. Attempting re-auth...")
             if not self.authenticate(): # authenticate acquires lock
                  logger.critical(f"Re-authentication failed. Cannot get item {media_item_id}.")
                  return None

        # Re-check authentication status (acquires lock)
        if not self.is_authenticated():
            logger.critical(f"Not authenticated after checks when getting item {media_item_id}.")
            return None

        # Acquire lock specifically for the API call
        with self._api_lock:
            # Ensure service object is available (check under lock)
            if not self.service:
                 logger.error(f"Service object not available when trying to get item {media_item_id}")
                 # Attempt rebuild (still under lock)
                 if not self._build_service():
                      logger.critical(f"Failed to rebuild service for get_media_item {media_item_id}")
                      return None

            try:
                # --- API Call (Protected by Lock) ---
                item = self.service.mediaItems().get(mediaItemId=media_item_id).execute()
                # --- End API Call ---
                logger.debug(f"Successfully retrieved details for media item ID: {media_item_id}")
                return item
            except HttpError as error:
                logger.error(f"HTTP error getting media item {media_item_id}: {error}")
                if error.resp.status == 401:
                    logger.error("401 Unauthorized getting item. Session might be invalid.")
                    # Invalidate session (still under lock)
                    self._invalidate_session()
                elif error.resp.status == 403: logger.error(f"403 Forbidden getting item {media_item_id}. Details: {error.content}")
                elif error.resp.status == 404: logger.error(f"404 Not Found for media item ID: {media_item_id}.")
                return None # Return None on HttpError
            except Exception as e:
                logger.error(f"Unexpected error getting media item {media_item_id}: {e}", exc_info=True)
                return None # Return None on other errors


    def download_photo(self, item_dict_from_db):
        """
        Downloads a photo/video item using its baseUrl from the provided dictionary.
        Handles missing/expired baseUrl and token issues internally.
        Lock acquired internally only for get_media_item calls.
        """
        if not isinstance(item_dict_from_db, dict):
            logger.error(f"Invalid item data type for download: {type(item_dict_from_db)}")
            return None, None, None, None
        if not self.temp_dir:
            logger.error("Temporary directory not initialized. Cannot download.")
            return None, None, None, None

        media_item_id = item_dict_from_db.get('google_id')
        filename = item_dict_from_db.get('filename')
        mime_type = item_dict_from_db.get('mime_type')
        base_url = item_dict_from_db.get('baseUrl')

        if not media_item_id or not filename or not mime_type:
             logger.error(f"Item data from DB missing required fields for download: {media_item_id}")
             return None, None, None, None

        logger.info(f"Preparing download for '{filename}' (ID: {media_item_id})")

        max_retries = 3
        retry_delay_seconds = 5
        refreshed_details_to_return = None

        # --- Ensure baseUrl is available BEFORE first download attempt ---
        if not base_url:
            logger.warning(f"Download '{filename}': BaseUrl missing. Fetching fresh details...")
            # get_media_item handles its own locking
            fresh_details = self.get_media_item(media_item_id)
            if fresh_details and fresh_details.get('baseUrl'):
                logger.info(f"Download '{filename}': Fetched fresh details with baseUrl.")
                base_url = fresh_details.get('baseUrl')
                refreshed_details_to_return = fresh_details
            else:
                logger.error(f"Download '{filename}': Failed fetch details with valid baseUrl. Aborting.")
                return None, None, None, None
        else:
             logger.debug(f"Download '{filename}': Using provided baseUrl from DB.")


        # --- Download Loop ---
        for attempt in range(max_retries):
            # --- Ensure Authentication (before network request) ---
            # refresh_token_if_needed handles locking
            if not self.refresh_token_if_needed():
                logger.error(f"Download '{filename}': Auth failed before attempt {attempt + 1}.")
                # authenticate handles locking
                if not self.authenticate():
                     logger.critical(f"Download '{filename}': Re-auth failed. Aborting.")
                     return None, None, None, None
                else:
                     logger.info(f"Download '{filename}': Re-auth successful.")

            if not base_url:
                 logger.error(f"Download '{filename}': BaseUrl missing before attempt {attempt + 1}. Aborting.")
                 return None, None, None, None

            # --- Construct Download URL ---
            is_video = mime_type.startswith('video/')
            download_param = "=dv" if is_video else "=d"
            download_url = base_url + download_param

            # --- Prepare Temporary File Path ---
            temp_file_path = None
            try:
                safe_filename = "".join(c for c in filename if c.isalnum() or c in ('.', '_', '-')).rstrip()
                if not safe_filename: safe_filename = f"item_{media_item_id}"
                temp_file_path = os.path.join(self.temp_dir, f"dl_{int(time.time()*1000)}_{safe_filename}")
            except Exception as e:
                 logger.error(f"Download '{filename}': Failed create temp path: {e}", exc_info=True)
                 return None, None, None, None

            # --- Perform Download Attempt (No lock needed for requests.get) ---
            logger.debug(f"Download '{filename}': Attempt {attempt + 1}/{max_retries} using URL ending ...{download_param}")
            last_status_code = None
            try:
                with requests.Session() as s:
                     response = s.get(download_url, stream=True, timeout=180)
                     last_status_code = response.status_code
                     response.raise_for_status()

                     with open(temp_file_path, 'wb') as f:
                         shutil.copyfileobj(response.raw, f, length=16*1024*1024)

                # --- Success ---
                logger.info(f"Successfully downloaded '{filename}' to {temp_file_path} on attempt {attempt + 1}")
                return temp_file_path, filename, mime_type, refreshed_details_to_return

            except requests.exceptions.HTTPError as http_err:
                status_code = last_status_code if last_status_code is not None else (http_err.response.status_code if http_err.response is not None else 'Unknown')
                logger.warning(f"Download '{filename}': HTTP Error attempt {attempt + 1}: {http_err} (Status: {status_code})")
                # ... (rest of error handling: 401, 403, 429, etc.) ...
                if status_code == 401:
                    logger.warning(f"Download '{filename}': 401 Unauthorized. Attempting token refresh...")
                    if not self.refresh_token_if_needed(buffer_minutes=-1): # Handles lock
                         logger.error(f"Download '{filename}': Token refresh failed after 401. Aborting.")
                         break
                    else:
                         logger.info(f"Download '{filename}': Token refreshed. Retrying download...")
                         continue # Retry loop

                elif status_code == 403:
                     logger.warning(f"Download '{filename}': 403 Forbidden (fallback). Refreshing item details...")
                     # get_media_item handles lock
                     refetched_item = self.get_media_item(media_item_id)
                     if refetched_item and refetched_item.get('baseUrl'):
                          logger.info(f"Download '{filename}': Refreshed item details successfully after 403.")
                          base_url = refetched_item.get('baseUrl') # Update baseUrl for the *next* attempt
                          refreshed_details_to_return = refetched_item # Mark for return
                          logger.info(f"Download '{filename}': Retrying download with new baseUrl...")
                          continue # Retry loop
                     else:
                          logger.error(f"Download '{filename}': Failed fallback refetch after 403. Aborting.")
                          break

                elif status_code == 429 or (isinstance(status_code, int) and 500 <= status_code <= 599):
                     if attempt < max_retries - 1:
                          logger.warning(f"Download '{filename}': {status_code}. Retrying after {retry_delay_seconds}s...")
                          time.sleep(retry_delay_seconds)
                          continue
                     else:
                          logger.error(f"Download '{filename}': {status_code}. Max retries reached.")
                          break
                else:
                    logger.error(f"Download '{filename}': Unhandled/non-retryable HTTP error ({status_code}). Aborting.")
                    break

            except requests.exceptions.RequestException as req_err:
                 log_level = logging.WARNING if attempt < max_retries - 1 else logging.ERROR
                 logger.log(log_level, f"Download '{filename}': Network/Request Error attempt {attempt + 1}: {req_err}", exc_info=True)
                 if attempt < max_retries - 1:
                      logger.info(f"Retrying download after network error in {retry_delay_seconds}s...")
                      time.sleep(retry_delay_seconds)
                      continue
                 else:
                      logger.error(f"Download '{filename}': Max retries after network errors.")
                      break

            except IOError as io_err:
                 logger.error(f"Download '{filename}': IO Error writing {temp_file_path}: {io_err}", exc_info=True)
                 break
            except Exception as e:
                 logger.error(f"Download '{filename}': Unexpected error attempt {attempt + 1}: {e}", exc_info=True)
                 break

        # --- After Loop ---
        logger.error(f"Failed to download '{filename}' (ID: {media_item_id}) after {max_retries} attempts.")
        if temp_file_path and os.path.exists(temp_file_path):
            try: os.remove(temp_file_path); logger.debug(f"Removed failed download file: {temp_file_path}")
            except OSError as e: logger.warning(f"Could not remove failed temp file {temp_file_path}: {e}")
        return None, None, None, None # Failure tuple

    def remove_photo(self, media_item_id, dry_run=False):
        """Placeholder for removing a photo. Google Photos API currently does not support deletion."""
        # Check authentication status first (acquires lock)
        if not self.is_authenticated():
             logger.error(f"Cannot {'simulate removing' if dry_run else 'remove'} item {media_item_id}: Not authenticated.")
             return False

        log_prefix = "[DRY RUN] Would remove" if dry_run else "[API UNSUPPORTED] Would attempt to remove"
        logger.info(f"{log_prefix} item {media_item_id} from Google Photos (Deletion via API not supported).")
        return True

    def cleanup_temp_dir(self):
        """Removes the temporary directory created by this instance if it exists."""
        temp_dir_path = getattr(self, 'temp_dir', None)
        logger.debug(f"Cleanup initiated for temp directory: {temp_dir_path}")
        if temp_dir_path and os.path.isdir(temp_dir_path):
            try:
                shutil.rmtree(temp_dir_path)
                logger.info(f"Successfully removed temporary directory: {temp_dir_path}")
                self.temp_dir = None
            except Exception as e:
                logger.error(f"Error removing temporary directory {temp_dir_path}: {e}", exc_info=True)
        elif temp_dir_path:
             logger.debug(f"Temp dir path '{temp_dir_path}' not found or not a directory.")
             self.temp_dir = None
        else:
            logger.debug("No temp dir path attribute found during cleanup.")

    def __del__(self):
         """Ensure temporary directory is cleaned up when the object is garbage collected."""
         logger.debug(f"GooglePhotos object ({id(self)}) being deleted, ensuring temp dir cleanup...")
         self.cleanup_temp_dir()

