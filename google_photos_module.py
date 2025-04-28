# google_photos_module.py (v2.0 - Quota Exit)
# - Removes exponential backoff retry logic.
# - Sets a shared event flag and returns on 429 errors.

# Standard library imports
import logging
import os
import tempfile
import shutil
import time
import datetime
import json
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
                 batch_size=50, quota_flag=None): # Add quota_flag parameter
        """Initialize with the paths to Google Photos configuration files."""
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.batch_size = batch_size
        self.service = None
        self.creds = None
        self.temp_dir = None
        self._api_lock = threading.Lock()
        self.quota_flag = quota_flag # Store the shared event flag

        try:
            self.temp_dir = tempfile.mkdtemp(prefix="gp2sm_")
            logger.debug(f"Created temporary directory: {self.temp_dir}")
        except Exception as e:
             logger.error(f"Failed to create temporary directory: {e}", exc_info=True)
             self.temp_dir = None

        try:
            if not self.authenticate():
                 raise RuntimeError("Google Photos authentication failed during initialization.")
        except GoogleCredentialsNotFoundError:
            raise
        except Exception as e:
             logger.error(f"An unexpected error occurred during Google Photos initialization: {e}", exc_info=True)
             raise

    # --- is_authenticated, authenticate, refresh_token_if_needed, _build_service, _invalidate_session ---
    # (Keep these methods exactly as they were in the previous version - no changes needed here)
    def is_authenticated(self):
        """Returns True if authenticated with Google Photos, False otherwise."""
        with self._api_lock:
            return self.service is not None and self.creds and self.creds.valid

    def authenticate(self):
        """Handles the OAuth 2.0 flow and service building."""
        with self._api_lock:
            creds = None
            if os.path.exists(self.token_file):
                try:
                    creds = Credentials.from_authorized_user_file(self.token_file, self.SCOPES)
                    logger.debug(f"Loaded credentials from {self.token_file}")
                except Exception as e:
                    logger.warning(f"Error loading token file {self.token_file}: {e}. Re-auth needed.")
                    creds = None

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    logger.info("Google Photos token expired. Refreshing...")
                    try:
                        creds.refresh(Request())
                        logger.info("Token refreshed successfully.")
                        try:
                            with open(self.token_file, 'w') as token:
                                token.write(creds.to_json())
                            logger.debug(f"Saved refreshed token to {self.token_file}")
                        except IOError as e:
                            logger.error(f"Error saving refreshed token: {e}")
                    except Exception as e:
                        logger.error(f"Error refreshing token: {e}. Manual re-auth required.", exc_info=True)
                        creds = None
                        if os.path.exists(self.token_file):
                            try: os.remove(self.token_file); logger.info(f"Removed invalid token file: {self.token_file}")
                            except OSError as rm_err: logger.warning(f"Could not remove token file {self.token_file}: {rm_err}")

                if not creds or not creds.valid:
                    logger.info("Starting authentication flow...")
                    try:
                        if not os.path.exists(self.credentials_file):
                             logger.critical(f"Credentials file not found: {self.credentials_file}")
                             raise GoogleCredentialsNotFoundError(f"Credentials file '{self.credentials_file}' missing.")

                        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.SCOPES)
                        creds = flow.run_local_server(port=0)
                        logger.info("Auth flow completed.")
                        try:
                            with open(self.token_file, 'w') as token:
                                token.write(creds.to_json())
                            logger.info(f"Saved new token to {self.token_file}")
                        except IOError as e:
                            logger.error(f"Error saving new token: {e}")

                    except GoogleCredentialsNotFoundError: raise
                    except Exception as flow_err:
                        logger.critical(f"OAuth flow failed: {flow_err}", exc_info=True)
                        self.creds = None; self.service = None; return False

            if creds and creds.valid:
                self.creds = creds
                return self._build_service()
            else:
                 logger.critical("Failed to obtain valid Google Photos credentials.")
                 self.creds = None; self.service = None; return False

    def refresh_token_if_needed(self, buffer_minutes=10):
        """Checks token expiry and refreshes if needed. Returns True if valid/refreshed, False otherwise."""
        with self._api_lock:
            if not self.creds:
                logger.warning("Cannot refresh token: No credentials.")
                return False

            if self.creds.valid and not self.creds.expiry:
                logger.debug("Creds valid, no expiry info.")
                return True

            if not self.creds.valid and self.creds.refresh_token:
                logger.info("Creds invalid/expired. Attempting refresh...")
                try:
                    self.creds.refresh(Request())
                    logger.info("Token refreshed.")
                    try:
                        with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                        logger.debug(f"Saved refreshed token.")
                    except IOError as e: logger.error(f"Error saving refreshed token: {e}")
                    if not self.service:
                         if not self._build_service(): return False
                    return True
                except Exception as e:
                    logger.error(f"Error refreshing token: {e}.", exc_info=True)
                    self._invalidate_session()
                    return False

            elif self.creds.valid and self.creds.expiry:
                expiry_dt = self.creds.expiry
                expiry_aware_dt = expiry_dt.astimezone(datetime.timezone.utc) if expiry_dt.tzinfo else expiry_dt.replace(tzinfo=datetime.timezone.utc)
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                refresh_time = expiry_aware_dt - datetime.timedelta(minutes=buffer_minutes)

                if now_utc >= refresh_time:
                    logger.info(f"Token expires soon ({expiry_aware_dt}). Proactive refresh...")
                    if self.creds.refresh_token:
                        try:
                            self.creds.refresh(Request())
                            logger.info("Proactive refresh successful.")
                            try:
                                with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                                logger.debug(f"Saved proactively refreshed token.")
                            except IOError as e: logger.error(f"Error saving proactively refreshed token: {e}")
                            if not self.service:
                                 if not self._build_service(): return False
                            return True
                        except Exception as e:
                            logger.error(f"Error proactive refresh: {e}.", exc_info=True)
                            self._invalidate_session()
                            return False
                    else:
                         logger.warning("Token needs proactive refresh, but no refresh token.")
                         return True
                else:
                     return True

            elif not self.creds.valid and not self.creds.refresh_token:
                 logger.error("Credentials invalid and no refresh token.")
                 self._invalidate_session()
                 return False

            return self.creds and self.creds.valid

    def _build_service(self):
        """Builds the Google Photos service object using credentials."""
        if not self.creds or not self.creds.valid:
            logger.error("Cannot build service: Invalid credentials.")
            self.service = None
            return False
        try:
            self.service = build(
                'photoslibrary', 'v1', credentials=self.creds,
                static_discovery=False, cache_discovery=False
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
    # --- End of unchanged methods ---

    def get_photos(self, album_id=None):
        """Retrieves a list of media item dictionaries. Sets quota flag on 429."""
        if not self.refresh_token_if_needed():
             logger.error("Token refresh/validation failed. Attempting re-authentication.")
             if not self.authenticate():
                  logger.critical("Re-authentication failed. Cannot retrieve photos.")
                  return []
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
                if not self.refresh_token_if_needed(buffer_minutes=1):
                     logger.error("Token became invalid mid-fetch. Stopping.")
                     break
                if not self.service:
                     logger.error("Google Photos service object missing mid-fetch.")
                     with self._api_lock:
                         if not self._build_service():
                              logger.critical("Failed to rebuild service mid-fetch. Stopping.")
                              break
                         else:
                              logger.info("Rebuilt service mid-fetch.")
                     if not self.service: break

                # --- API Call (No Retry Loop) ---
                results = None
                try:
                    if album_id:
                         body = {'albumId': album_id, 'pageSize': self.batch_size}
                         if nextPageToken: body['pageToken'] = nextPageToken
                         results = self.service.mediaItems().search(body=body).execute()
                    else:
                         results = self.service.mediaItems().list(pageSize=self.batch_size, pageToken=nextPageToken).execute()
                except HttpError as error:
                     if error.resp.status == 429:
                          logger.critical(f"Google API Quota (429) Exceeded during list fetch (page {page_count}). Stopping fetch.")
                          if self.quota_flag:
                               self.quota_flag.set() # Signal quota exceeded
                          # Return whatever items were fetched so far
                          logger.warning(f"Returning {len(all_items)} items fetched before quota error.")
                          return all_items # Return partial list
                     else:
                          raise # Re-raise other HttpErrors to be caught below
                # --- End API Call ---

                items = results.get('mediaItems')
                if not items:
                    logger.info(f"No more items found on page {page_count} for {action_desc}.")
                    break

                all_items.extend(items)
                num_items_in_batch = len(items)
                total_items_retrieved += num_items_in_batch
                logger.info(f"Retrieved {num_items_in_batch} items in batch {page_count}. Total so far: {total_items_retrieved}")

                nextPageToken = results.get('nextPageToken')
                if not nextPageToken:
                    logger.info(f"No more pages found for {action_desc}. Finished fetching.")
                    break

            except HttpError as error:
                logger.error(f'HTTP error retrieving photos page {page_count}: {error}')
                if error.resp.status == 401:
                    logger.error("401 Unauthorized during fetch. Session may be invalid. Stopping.")
                    self._invalidate_session()
                elif error.resp.status == 403: logger.error(f"403 Forbidden during fetch. Check permissions/quota. Details: {error.content}")
                elif error.resp.status == 404 and album_id: logger.error(f"404 Not Found for album ID '{album_id}'. Check ID."); all_items = []
                else: logger.error(f"Unhandled HTTP Error ({error.resp.status}) during fetch. Stopping.")
                break
            except Exception as e:
                logger.error(f'Unexpected error retrieving photos page {page_count}: {e}', exc_info=True)
                break

        logger.info(f"Finished fetching from {action_desc}. Total items retrieved: {len(all_items)}")
        return all_items

    def get_media_item(self, media_item_id):
        """
        Retrieves the full details of a single media item by its ID.
        Sets quota flag and returns None on 429 error. No retries.
        """
        if not media_item_id:
             logger.error("Cannot get media item: No media_item_id provided.")
             return None

        logger.debug(f"Attempting to get details for media item ID: {media_item_id}")

        if not self.refresh_token_if_needed():
             logger.error(f"Token invalid before getting item {media_item_id}. Re-auth...")
             if not self.authenticate():
                  logger.critical(f"Re-authentication failed. Cannot get item {media_item_id}.")
                  return None
        if not self.is_authenticated():
            logger.critical(f"Not authenticated after checks when getting item {media_item_id}.")
            return None

        with self._api_lock:
            if not self.service:
                 logger.error(f"Service object not available when trying to get item {media_item_id}")
                 if not self._build_service():
                      logger.critical(f"Failed to rebuild service for get_media_item {media_item_id}")
                      return None
            try:
                item = self.service.mediaItems().get(mediaItemId=media_item_id).execute()
                logger.debug(f"Successfully retrieved details for media item ID: {media_item_id}")
                return item
            except HttpError as error:
                logger.warning(f"HTTP error getting media item {media_item_id}: {error}")
                if error.resp.status == 401:
                    logger.error("401 Unauthorized getting item. Session might be invalid.")
                    self._invalidate_session()
                elif error.resp.status == 403:
                    logger.error(f"403 Forbidden getting item {media_item_id}. Details: {error.content}")
                elif error.resp.status == 404:
                    logger.error(f"404 Not Found for media item ID: {media_item_id}.")
                elif error.resp.status == 429:
                    # --- Quota Exceeded Handling ---
                    logger.critical(f"Google API Quota (429) Exceeded getting item {media_item_id}. Aborting operation.")
                    if self.quota_flag:
                        self.quota_flag.set() # Signal quota exceeded globally
                    # --- End Quota Handling ---
                else:
                    logger.error(f"Unhandled HTTP Error ({error.resp.status}) getting item {media_item_id}.")
                return None # Return None on HttpError
            except Exception as e:
                logger.error(f"Unexpected error getting media item {media_item_id}: {e}", exc_info=True)
                return None
        # Lock released here
        return None # Should not be reached unless error occurred

    # --- download_photo, remove_photo, cleanup_temp_dir, __del__ ---
    # (Keep these methods exactly as they were in the previous version - no changes needed here)
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
            # get_media_item handles its own locking and retries/quota check
            fresh_details = self.get_media_item(media_item_id)
            if fresh_details and fresh_details.get('baseUrl'):
                logger.info(f"Download '{filename}': Fetched fresh details with baseUrl.")
                base_url = fresh_details.get('baseUrl')
                refreshed_details_to_return = fresh_details # Store to return later
            else:
                # If get_media_item returned None, it might be due to quota or other error
                logger.error(f"Download '{filename}': Failed fetch details (baseUrl missing or get_media_item failed). Aborting download.")
                return None, None, None, None # Indicate download failure
        else:
             logger.debug(f"Download '{filename}': Using provided baseUrl from DB.")


        # --- Download Loop ---
        for attempt in range(max_retries):
            # --- Ensure Authentication (before network request) ---
            if not self.refresh_token_if_needed():
                logger.error(f"Download '{filename}': Auth failed before attempt {attempt + 1}.")
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

            # --- Perform Download Attempt ---
            logger.debug(f"Download '{filename}': Attempt {attempt + 1}/{max_retries} using URL ending ...{download_param}")
            last_status_code = None
            try:
                with requests.Session() as s:
                     auth_header = f"Bearer {self.creds.token}"
                     response = s.get(download_url, stream=True, timeout=180, headers={'Authorization': auth_header})
                     last_status_code = response.status_code
                     response.raise_for_status()
                     with open(temp_file_path, 'wb') as f:
                         shutil.copyfileobj(response.raw, f, length=16*1024*1024)
                logger.info(f"Successfully downloaded '{filename}' to {temp_file_path} on attempt {attempt + 1}")
                return temp_file_path, filename, mime_type, refreshed_details_to_return

            except requests.exceptions.HTTPError as http_err:
                status_code = last_status_code if last_status_code is not None else (http_err.response.status_code if http_err.response is not None else 'Unknown')
                logger.warning(f"Download '{filename}': HTTP Error attempt {attempt + 1}: {http_err} (Status: {status_code})")
                if status_code == 401:
                    logger.warning(f"Download '{filename}': 401 Unauthorized. Refreshing item details...")
                    refetched_item = self.get_media_item(media_item_id) # Handles quota check
                    if refetched_item and refetched_item.get('baseUrl'):
                         logger.info(f"Download '{filename}': Refreshed item details successfully after 401.")
                         base_url = refetched_item.get('baseUrl')
                         refreshed_details_to_return = refetched_item
                         logger.info(f"Download '{filename}': Retrying download with new baseUrl...")
                         continue
                    else: logger.error(f"Download '{filename}': Failed refetch after 401. Aborting."); break
                elif status_code == 403:
                     logger.warning(f"Download '{filename}': 403 Forbidden. Refreshing item details...")
                     refetched_item = self.get_media_item(media_item_id) # Handles quota check
                     if refetched_item and refetched_item.get('baseUrl'):
                          logger.info(f"Download '{filename}': Refreshed item details successfully after 403.")
                          base_url = refetched_item.get('baseUrl')
                          refreshed_details_to_return = refetched_item
                          logger.info(f"Download '{filename}': Retrying download with new baseUrl...")
                          continue
                     else: logger.error(f"Download '{filename}': Failed refetch after 403. Aborting."); break
                elif status_code == 429: # Quota exceeded on download itself (less likely but possible)
                     logger.critical(f"Google API Quota (429) Exceeded during download for '{filename}'. Aborting download.")
                     if self.quota_flag: self.quota_flag.set()
                     break # Stop trying to download this item
                elif (isinstance(status_code, int) and 500 <= status_code <= 599):
                     if attempt < max_retries - 1:
                          wait_time = (retry_delay_seconds * (2 ** attempt)) + random.uniform(0, 1)
                          logger.warning(f"Download '{filename}': {status_code}. Retrying after {wait_time:.2f}s...")
                          time.sleep(wait_time); continue
                     else: logger.error(f"Download '{filename}': {status_code}. Max retries reached."); break
                else: logger.error(f"Download '{filename}': Unhandled/non-retryable HTTP error ({status_code}). Aborting."); break
            except requests.exceptions.RequestException as req_err:
                 log_level = logging.WARNING if attempt < max_retries - 1 else logging.ERROR
                 logger.log(log_level, f"Download '{filename}': Network/Request Error attempt {attempt + 1}: {req_err}", exc_info=True)
                 if attempt < max_retries - 1:
                      wait_time = (retry_delay_seconds * (2 ** attempt)) + random.uniform(0, 1)
                      logger.info(f"Retrying download after network error in {wait_time:.2f}s...")
                      time.sleep(wait_time); continue
                 else: logger.error(f"Download '{filename}': Max retries after network errors."); break
            except IOError as io_err: logger.error(f"Download '{filename}': IO Error writing {temp_file_path}: {io_err}", exc_info=True); break
            except Exception as e: logger.error(f"Download '{filename}': Unexpected error attempt {attempt + 1}: {e}", exc_info=True); break

        # --- After Loop ---
        logger.error(f"Failed to download '{filename}' (ID: {media_item_id}) after {max_retries} attempts.")
        if temp_file_path and os.path.exists(temp_file_path):
            try: os.remove(temp_file_path); logger.debug(f"Removed failed download file: {temp_file_path}")
            except OSError as e: logger.warning(f"Could not remove failed temp file {temp_file_path}: {e}")
        return None, None, None, None # Failure tuple

    def remove_photo(self, media_item_id, dry_run=False):
        """Placeholder for removing a photo."""
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

