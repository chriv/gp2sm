# google_photos_module.py (v1.9)
# Download_photo now robustly handles missing/expired baseUrl internally.

# Standard library imports
import logging
import os
import tempfile
import shutil
import time # Needed for sleep
import datetime # Needed for expiry check
import json # For handling metadata

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
        return self.service is not None and self.creds and self.creds.valid

    def authenticate(self):
        """Handles the OAuth 2.0 flow and service building."""
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
                        # Continue, but might need auth again next time
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
                         # Specific check for the credentials file
                         logger.critical(f"Google API credentials file not found: {self.credentials_file}")
                         logger.critical("Please download your OAuth 2.0 Client ID JSON from Google Cloud Console,")
                         logger.critical(f"rename it to '{self.credentials_file}', and place it in the script's directory.")
                         raise GoogleCredentialsNotFoundError(f"Credentials file '{self.credentials_file}' is missing.")

                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.SCOPES)
                    # run_local_server will open browser, handle auth, get code, and return creds
                    creds = flow.run_local_server(port=0)
                    logger.info("Authentication flow completed successfully.")
                    # Save the new credentials for future use
                    try:
                        with open(self.token_file, 'w') as token:
                            token.write(creds.to_json())
                        logger.info(f"Saved new token to {self.token_file}")
                    except IOError as e:
                        logger.error(f"Error saving new token to {self.token_file}: {e}")

                except GoogleCredentialsNotFoundError:
                     # Re-raise the specific error if the credentials file itself is missing
                     raise
                except Exception as flow_err:
                    logger.critical(f"Google Photos OAuth flow failed: {flow_err}", exc_info=True)
                    self.creds = None
                    self.service = None
                    return False # Indicate failure

        # If we have valid credentials (either loaded, refreshed, or newly obtained)
        if creds and creds.valid:
            self.creds = creds
            # Use helper to build service, returns True/False
            return self._build_service()
        else:
             # If after all attempts, creds are still not valid
             logger.critical("Failed to obtain valid Google Photos credentials after all attempts.")
             self.creds = None
             self.service = None
             return False # Indicate failure

    def refresh_token_if_needed(self, buffer_minutes=10):
        """Checks token expiry and refreshes if needed. Returns True if valid/refreshed, False otherwise."""
        if not self.creds:
            logger.warning("Cannot refresh token: No credentials loaded.")
            return False # No creds to refresh

        # Case 1: Credentials are valid and have no expiry info (unlikely with OAuth 2.0, but handle defensively)
        if self.creds.valid and not self.creds.expiry:
            logger.debug("Credentials valid but have no expiry information. Assuming OK.")
            return True

        # Case 2: Credentials invalid, but refresh token exists
        if not self.creds.valid and self.creds.refresh_token:
            logger.info("Credentials invalid or expired. Attempting immediate refresh...")
            try:
                self.creds.refresh(Request())
                logger.info("Token refreshed successfully.")
                try:
                    with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                    logger.debug(f"Saved refreshed token to {self.token_file}")
                except IOError as e: logger.error(f"Error saving refreshed token: {e}")
                # Re-build service if necessary (might have been invalidated)
                if not self.service:
                     if not self._build_service():
                          logger.error("Failed to rebuild service after token refresh.")
                          return False # Indicate failure if service cannot be rebuilt
                return True # Refresh successful
            except Exception as e:
                logger.error(f"Error during token refresh: {e}.", exc_info=True)
                self._invalidate_session() # Invalidate session on refresh failure
                return False # Refresh failed

        # Case 3: Credentials valid, check expiry time against buffer
        elif self.creds.valid and self.creds.expiry:
            # Ensure expiry is timezone-aware (UTC)
            expiry_dt = self.creds.expiry
            if expiry_dt.tzinfo is None:
                 expiry_aware_dt = expiry_dt.replace(tzinfo=datetime.timezone.utc)
            else:
                 # Already aware, ensure it's UTC
                 expiry_aware_dt = expiry_dt.astimezone(datetime.timezone.utc)

            now_utc = datetime.datetime.now(datetime.timezone.utc)
            time_until_expiry = expiry_aware_dt - now_utc
            refresh_threshold = datetime.timedelta(minutes=buffer_minutes)

            if time_until_expiry <= refresh_threshold:
                logger.info(f"Token expires in {time_until_expiry}. Refreshing proactively (buffer: {buffer_minutes} mins)...")
                if self.creds.refresh_token:
                    try:
                        self.creds.refresh(Request())
                        logger.info("Proactive token refresh successful.")
                        try:
                            with open(self.token_file, 'w') as token: token.write(self.creds.to_json())
                            logger.debug(f"Saved proactively refreshed token.")
                        except IOError as e: logger.error(f"Error saving proactively refreshed token: {e}")
                        # Re-build service if necessary
                        if not self.service:
                             if not self._build_service():
                                  logger.error("Failed to rebuild service after proactive refresh.")
                                  return False
                        return True # Proactive refresh succeeded
                    except Exception as e:
                        logger.error(f"Error during proactive token refresh: {e}.", exc_info=True)
                        self._invalidate_session() # Invalidate on failure
                        return False # Proactive refresh failed
                else:
                     logger.warning("Token needs proactive refresh, but no refresh token available. Cannot refresh.")
                     # Token is still valid for now, but might expire soon.
                     return True
            else:
                 # logger.debug(f"Token is valid and not within refresh buffer. Expires in {time_until_expiry}.") # Can be too verbose
                 return True # Token is fine

        # Case 4: Credentials invalid and no refresh token
        elif not self.creds.valid and not self.creds.refresh_token:
             logger.error("Credentials invalid and no refresh token available. Manual re-authentication required.")
             self._invalidate_session() # Ensure session is cleared
             return False # Cannot proceed

        # Default case (shouldn't be reached ideally)
        logger.warning("Reached unexpected state in refresh_token_if_needed.")
        return self.creds and self.creds.valid


    def _build_service(self):
        """Internal helper to build the service object."""
        if not self.creds or not self.creds.valid:
             logger.error("Cannot build service: Invalid credentials.")
             self.service = None # Ensure service is None if creds invalid
             return False
        try:
            # Explicitly disable cache discovery to avoid potential file system issues
            # or issues in environments where disk cache is not reliable/writable.
            self.service = build('photoslibrary', 'v1', credentials=self.creds, static_discovery=False, cache_discovery=False)
            logger.info("Google Photos API service (re)built successfully.")
            return True
        except Exception as build_err:
            logger.error(f"Failed to (re)build service: {build_err}", exc_info=True)
            self.service = None # Ensure service is None on build failure
            return False

    def _invalidate_session(self):
         """Internal helper to clear session state on critical auth errors."""
         logger.warning("Invalidating Google Photos session state.")
         self.creds = None
         self.service = None
         # Attempt to remove the potentially problematic token file
         if os.path.exists(self.token_file):
             try:
                  os.remove(self.token_file)
                  logger.info(f"Removed token file due to session invalidation: {self.token_file}")
             except OSError as rm_err:
                  logger.warning(f"Could not remove token file {self.token_file}: {rm_err}")


    def get_photos(self, album_id=None):
        """Retrieves a list of media item dictionaries from the library or a specific album."""
        # Ensure authenticated before proceeding
        if not self.refresh_token_if_needed():
             logger.error("Token refresh/validation failed. Attempting re-authentication.")
             if not self.authenticate():
                  logger.critical("Re-authentication failed. Cannot retrieve photos.")
                  return [] # Critical failure

        if not self.is_authenticated(): # Should be authenticated after the check/auth above
            logger.critical("Not authenticated after checks. Cannot retrieve photos.")
            return []

        all_items = []; nextPageToken = None; page_count = 0; total_items_retrieved = 0
        action_desc = f"album ID: {album_id}" if album_id else "library"
        logger.info(f"Starting fetch from Google Photos {action_desc}...")

        while True:
            page_count += 1
            logger.debug(f"Fetching page {page_count} for {action_desc}...")
            try:
                # Ensure token is still valid before making the API call
                if not self.refresh_token_if_needed():
                     logger.error("Token became invalid mid-fetch. Attempting re-auth...")
                     if not self.authenticate():
                         logger.critical("Re-authentication failed mid-fetch. Stopping photo retrieval.")
                         break # Exit loop if re-auth fails
                     else:
                          logger.info("Re-authenticated mid-fetch. Retrying page...")
                          page_count -= 1 # Decrement to retry the same page
                          continue

                # Ensure service object is available
                if not self.service:
                     logger.error("Google Photos service object is missing. Cannot fetch.")
                     break

                # Construct request body/parameters based on whether it's library or album
                if album_id:
                     body = {'albumId': album_id, 'pageSize': self.batch_size}
                     if nextPageToken: body['pageToken'] = nextPageToken
                     results = self.service.mediaItems().search(body=body).execute()
                else:
                     results = self.service.mediaItems().list(pageSize=self.batch_size, pageToken=nextPageToken).execute()

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
                logger.error(f'HTTP error retrieving photos from {action_desc} (Page {page_count}): {error}')
                # Handle specific HTTP errors
                if error.resp.status == 401:
                    logger.error("401 Unauthorized encountered during photo fetch. Session may be invalid. Stopping.")
                    self._invalidate_session() # Invalidate session on persistent 401
                    break
                elif error.resp.status == 403:
                    # 403 can mean permissions issue, quota exceeded, or other access problems.
                    logger.error(f"403 Forbidden encountered during photo fetch. Check API permissions/quota. Details: {error.content}")
                    break # Typically not recoverable by simple retry/refresh
                elif error.resp.status == 404 and album_id:
                     logger.error(f"404 Not Found for album ID '{album_id}'. Please check the ID.")
                     all_items = [] # Clear any potentially fetched items as the album is wrong
                     break
                elif error.resp.status == 429:
                     # Rate limiting
                     sleep_time = 60 # Default sleep time
                     # Check for Retry-After header (value is in seconds)
                     retry_after = error.resp.headers.get('Retry-After')
                     if retry_after:
                          try:
                               sleep_time = int(retry_after) + 5 # Add a small buffer
                               logger.warning(f"429 Too Many Requests. Respecting Retry-After header. Sleeping for {sleep_time} seconds...")
                          except ValueError:
                               logger.warning(f"429 Too Many Requests. Invalid Retry-After header value: '{retry_after}'. Sleeping for default {sleep_time} seconds...")
                     else:
                          logger.warning(f"429 Too Many Requests. No Retry-After header. Sleeping for default {sleep_time} seconds...")

                     time.sleep(sleep_time)
                     page_count -=1 # Retry the same page
                     continue # Retry the request
                else:
                    logger.error(f"Unhandled HTTP Error ({error.resp.status}) during photo fetch. Stopping.")
                    break # Stop on other unhandled HTTP errors

            except Exception as e:
                logger.error(f'Unexpected error retrieving photos from {action_desc} (Page {page_count}): {e}', exc_info=True)
                break # Stop on any other unexpected errors

        logger.info(f"Finished fetching from {action_desc}. Total items retrieved: {len(all_items)}")
        return all_items

    def get_media_item(self, media_item_id):
        """Retrieves the full details of a single media item by its ID."""
        if not media_item_id:
             logger.error("Cannot get media item: No media_item_id provided.")
             return None

        logger.debug(f"Attempting to get details for media item ID: {media_item_id}")
        # Ensure authenticated before proceeding
        if not self.refresh_token_if_needed():
             logger.error(f"Token invalid before getting item {media_item_id}. Attempting re-auth...")
             if not self.authenticate():
                  logger.critical(f"Re-authentication failed. Cannot get item {media_item_id}.")
                  return None

        if not self.is_authenticated() or not self.service:
            logger.critical(f"Not authenticated or service unavailable when getting item {media_item_id}.")
            return None

        try:
            # Call the API to get item details
            item = self.service.mediaItems().get(mediaItemId=media_item_id).execute()
            logger.debug(f"Successfully retrieved details for media item ID: {media_item_id}")
            return item
        except HttpError as error:
            logger.error(f"HTTP error getting media item {media_item_id}: {error}")
            if error.resp.status == 401:
                logger.error("401 Unauthorized getting item. Session might be invalid.")
                self._invalidate_session() # Invalidate session on 401
            elif error.resp.status == 403:
                 logger.error(f"403 Forbidden getting item {media_item_id}. Check permissions. Details: {error.content}")
            elif error.resp.status == 404:
                logger.error(f"404 Not Found for media item ID: {media_item_id}. Item may not exist or be accessible.")
            # Other errors logged generically above
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting media item {media_item_id}: {e}", exc_info=True)
            return None

    def download_photo(self, item_dict_from_db):
        """
        Downloads a photo/video item using its baseUrl from the provided dictionary.
        Handles missing/expired baseUrl and token issues internally.

        Args:
            item_dict_from_db (dict): Dictionary containing item details from the DB
                                      (must include 'google_id', 'filename', 'mimeType').
                                      'baseUrl' is optional but preferred.

        Returns tuple:
            On success: (temp_file_path, filename, mime_type, refreshed_item_details_dict or None)
                       refreshed_item_details_dict contains full details if a refresh occurred, else None.
            On failure: (None, None, None, None)
        """
        if not isinstance(item_dict_from_db, dict):
            logger.error(f"Invalid item data type for download: {type(item_dict_from_db)}")
            return None, None, None, None
        if not self.temp_dir:
            logger.error("Temporary directory not initialized. Cannot download.")
            return None, None, None, None

        # --- Extract necessary info ---
        media_item_id = item_dict_from_db.get('google_id') # Use google_id from DB
        filename = item_dict_from_db.get('filename')
        mime_type = item_dict_from_db.get('mime_type')
        base_url = item_dict_from_db.get('baseUrl') # Get initial baseUrl from DB dict

        if not media_item_id or not filename or not mime_type:
             logger.error(f"Item data from DB missing required fields (google_id, filename, mimeType) for download: {media_item_id}")
             return None, None, None, None

        logger.info(f"Preparing download for '{filename}' (ID: {media_item_id})")

        max_retries = 3
        retry_delay_seconds = 5
        refreshed_details_to_return = None # Store refreshed details if they occur

        # --- Ensure baseUrl is available BEFORE first download attempt ---
        # This is the core logic change: handle missing/stale baseUrl here.
        if not base_url:
            logger.warning(f"Download '{filename}': BaseUrl missing in provided details. Fetching fresh details...")
            fresh_details = self.get_media_item(media_item_id) # Use internal method
            if fresh_details and fresh_details.get('baseUrl'):
                logger.info(f"Download '{filename}': Successfully fetched fresh details with baseUrl.")
                base_url = fresh_details.get('baseUrl')
                # Mark these details to be returned so DB can be updated
                refreshed_details_to_return = fresh_details
            else:
                logger.error(f"Download '{filename}': Failed to fetch details with a valid baseUrl. Aborting download.")
                # No need to update DB status here, main loop will handle the None return
                return None, None, None, None
        else:
             logger.debug(f"Download '{filename}': Using provided baseUrl from DB.")


        # --- Download Loop ---
        for attempt in range(max_retries):
            # --- Ensure Authentication ---
            # Authentication check remains important before each network attempt
            if not self.refresh_token_if_needed():
                logger.error(f"Download '{filename}': Auth failed before attempt {attempt + 1}.")
                if not self.authenticate():
                     logger.critical(f"Download '{filename}': Re-auth failed. Aborting.")
                     return None, None, None, None
                else:
                     logger.info(f"Download '{filename}': Re-auth successful, continuing attempt {attempt+1}.")

            # At this point, base_url *should* be populated from the initial check or DB
            if not base_url:
                 # This indicates a logic error if reached after the initial check
                 logger.error(f"Download '{filename}': BaseUrl is unexpectedly missing before attempt {attempt + 1}. Aborting.")
                 return None, None, None, None

            # --- Construct Download URL ---
            is_video = mime_type.startswith('video/')
            download_param = "=dv" if is_video else "=d"
            download_url = base_url + download_param

            # --- Prepare Temporary File Path ---
            temp_file_path = None
            try:
                # Sanitize filename
                safe_filename = "".join(c for c in filename if c.isalnum() or c in ('.', '_', '-')).rstrip()
                if not safe_filename: safe_filename = f"item_{media_item_id}" # Fallback
                temp_file_path = os.path.join(self.temp_dir, f"dl_{int(time.time()*1000)}_{safe_filename}")
                # logger.debug(f"Download '{filename}': Temp path: {temp_file_path}") # Too verbose
            except Exception as e:
                 logger.error(f"Download '{filename}': Failed create temp path: {e}", exc_info=True)
                 return None, None, None, None # Cannot proceed

            # --- Perform Download Attempt ---
            logger.debug(f"Download '{filename}': Attempt {attempt + 1}/{max_retries} using URL ending ...{download_param}")
            last_status_code = None
            try:
                # Use requests library for download
                with requests.Session() as s:
                     # Note: Google download URLs are usually pre-signed and don't need OAuth tokens in header
                     response = s.get(download_url, stream=True, timeout=180) # Increased timeout
                     last_status_code = response.status_code
                     response.raise_for_status() # Raise HTTPError for 4xx/5xx

                     # Stream content to file
                     with open(temp_file_path, 'wb') as f:
                         shutil.copyfileobj(response.raw, f, length=16*1024*1024) # 16MB buffer

                # --- Success ---
                logger.info(f"Successfully downloaded '{filename}' to {temp_file_path} on attempt {attempt + 1}")
                # Return success tuple, including refreshed details if they were obtained earlier
                return temp_file_path, filename, mime_type, refreshed_details_to_return

            except requests.exceptions.HTTPError as http_err:
                # Handle HTTP errors (4xx, 5xx)
                status_code = last_status_code if last_status_code is not None else (http_err.response.status_code if http_err.response is not None else 'Unknown')
                logger.warning(f"Download '{filename}': HTTP Error attempt {attempt + 1}: {http_err} (Status: {status_code})")
                if logger.isEnabledFor(logging.DEBUG) and hasattr(http_err, 'response') and http_err.response is not None:
                     try: logger.debug(f"  Response Text: {http_err.response.text[:500]}...")
                     except Exception: logger.debug("  Response Text could not be read.")

                # --- Specific Handling for 401/403 ---
                if status_code == 401:
                    # Authentication error - likely token expired or invalid
                    logger.warning(f"Download '{filename}': 401 Unauthorized. Attempting token refresh...")
                    if not self.refresh_token_if_needed(buffer_minutes=-1): # Force immediate check/refresh
                         logger.error(f"Download '{filename}': Token refresh failed after 401. Aborting.")
                         break # Exit retry loop if refresh fails
                    else:
                         logger.info(f"Download '{filename}': Token refreshed. Retrying download...")
                         # Need to ensure service object is rebuilt if it was invalidated
                         if not self.service: self._build_service()
                         continue # Retry loop

                elif status_code == 403:
                     # Forbidden - likely expired baseUrl (even if checked initially)
                     logger.warning(f"Download '{filename}': 403 Forbidden (fallback). Refreshing item details...")
                     refetched_item = self.get_media_item(media_item_id) # Try getting fresh details again
                     if refetched_item and refetched_item.get('baseUrl'):
                          logger.info(f"Download '{filename}': Refreshed item details successfully after 403.")
                          base_url = refetched_item.get('baseUrl') # Update baseUrl for the *next* attempt in the loop
                          refreshed_details_to_return = refetched_item # Mark these newer details for return
                          logger.info(f"Download '{filename}': Retrying download with new baseUrl...")
                          continue # Retry loop with new baseUrl
                     else:
                          logger.error(f"Download '{filename}': Failed fallback refetch after 403 or new details lack baseUrl. Aborting.")
                          break # Exit retry loop if refetch fails

                # --- Handling for other retryable errors ---
                elif status_code == 429 or (isinstance(status_code, int) and 500 <= status_code <= 599):
                     # Rate limiting or server error
                     if attempt < max_retries - 1:
                          logger.warning(f"Download '{filename}': Received {status_code}. Retrying after {retry_delay_seconds}s...")
                          time.sleep(retry_delay_seconds)
                          continue # Retry the download
                     else:
                          logger.error(f"Download '{filename}': Received {status_code}. Max retries reached.")
                          break # Max retries reached for this error type
                else: # Non-retryable HTTP errors (e.g., 400, 404 on download URL)
                    logger.error(f"Download '{filename}': Unhandled/non-retryable HTTP error ({status_code}). Aborting download.")
                    break # Exit retry loop

            except requests.exceptions.RequestException as req_err:
                 # Network errors, timeouts, etc.
                 log_level = logging.WARNING if attempt < max_retries - 1 else logging.ERROR
                 logger.log(log_level, f"Download '{filename}': Network/Request Error attempt {attempt + 1}: {req_err}", exc_info=True)
                 if attempt < max_retries - 1:
                      logger.info(f"Retrying download after network error in {retry_delay_seconds}s...")
                      time.sleep(retry_delay_seconds)
                      continue
                 else:
                      logger.error(f"Download '{filename}': Max retries reached after network errors.")
                      break

            except IOError as io_err:
                 # Errors writing to the temporary file
                 logger.error(f"Download '{filename}': IO Error writing temporary file {temp_file_path}: {io_err}", exc_info=True)
                 break # Cannot recover from IO error
            except Exception as e:
                 # Catch-all for any other unexpected exceptions during download
                 logger.error(f"Download '{filename}': Unexpected error during download attempt {attempt + 1}: {e}", exc_info=True)
                 break # Abort on unexpected errors

        # --- After Loop ---
        # If the loop finishes without returning success, it means download failed.
        logger.error(f"Failed to download '{filename}' (ID: {media_item_id}) after {max_retries} attempts.")
        # Clean up the potentially partially downloaded or failed temporary file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Removed failed/partial download file: {temp_file_path}")
            except OSError as e:
                logger.warning(f"Could not remove failed/partial temp file {temp_file_path}: {e}")
        # Return the failure tuple
        return None, None, None, None

    def remove_photo(self, media_item_id, dry_run=False):
        """Placeholder for removing a photo. Google Photos API currently does not support deletion."""
        # Check authentication status first
        if not self.is_authenticated():
             logger.error(f"Cannot {'simulate removing' if dry_run else 'remove'} item {media_item_id}: Not authenticated.")
             return False # Indicate failure

        # Log the action based on dry_run flag
        log_prefix = "[DRY RUN] Would remove" if dry_run else "[API UNSUPPORTED] Would attempt to remove"
        logger.info(f"{log_prefix} item {media_item_id} from Google Photos (Deletion via API is not currently supported).")
        # Always return True for simulation/unsupported action
        return True

    def cleanup_temp_dir(self):
        """Removes the temporary directory created by this instance if it exists."""
        # Use getattr to safely access self.temp_dir, in case init failed partially
        temp_dir_path = getattr(self, 'temp_dir', None)
        logger.debug(f"Cleanup initiated for temp directory: {temp_dir_path}")
        if temp_dir_path and os.path.isdir(temp_dir_path): # Check if it's actually a directory
            try:
                shutil.rmtree(temp_dir_path)
                logger.info(f"Successfully removed temporary directory: {temp_dir_path}")
                self.temp_dir = None # Reset the attribute after successful removal
            except Exception as e:
                logger.error(f"Error removing temporary directory {temp_dir_path}: {e}", exc_info=True)
                # Don't reset self.temp_dir, it might still exist partially
        elif temp_dir_path:
             # Path exists but is not a directory, or was already removed
             logger.debug(f"Temporary directory path '{temp_dir_path}' not found or is not a directory during cleanup.")
             self.temp_dir = None # Reset attribute if path is invalid or gone
        else:
            logger.debug("No temporary directory path attribute found during cleanup (likely failed during init or already cleaned).")

    def __del__(self):
         """Ensure temporary directory is cleaned up when the object is garbage collected."""
         # Call the explicit cleanup method upon object deletion
         logger.debug(f"GooglePhotos object ({id(self)}) being deleted, ensuring temp dir cleanup...")
         self.cleanup_temp_dir()

