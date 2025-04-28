# SmugMug Module (v2.0)
# - Added detailed debug logging for !authuser response.
# - Added SmugMugAlbumFullError exception.
# - Modified upload_media to detect album full error (code 63) and raise exception.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#

# Standard library imports
import hashlib
import json
import logging
import os

# Third-party imports
import requests
from requests_oauthlib import OAuth1Session

# Get logger instance
logger = logging.getLogger(__name__)

# Define the default SmugMug config structure
DEFAULT_SMUGMUG_CONFIG = {
    "api_key": "YOUR_SMUGMUG_API_KEY",
    "api_secret": "YOUR_SMUGMUG_API_SECRET",
    "_comment_api": "Get API Key/Secret from https://api.smugmug.com/api/developer/apply",

    "oauth_token": "YOUR_SMUGMUG_OAUTH_TOKEN", # Leave as placeholder
    "oauth_token_secret": "YOUR_SMUGMUG_OAUTH_TOKEN_SECRET", # Leave as placeholder
    "_comment_oauth": "Leave oauth tokens as placeholders; script will fill them on first run.",

    "_comment_album_select": "--- IMPORTANT: Choose ONE method below to specify the target album ---",

    "_comment_album_name": "(Option 1) Specify album name (script will find or create it):",
    "album_name": "YOUR_SMUGMUG_ALBUM_NAME",

    "_comment_album_key": "(Option 2) Specify album key and URI (find key in album URL):",
    "album_key": "TARGET_SMUGMUG_ALBUM_KEY",
    "album_api_uri": "/api/v2/album/TARGET_SMUGMUG_ALBUM_KEY", # Example: /api/v2/album/ABCDE

    "_comment_folder": "(Optional) Specify folder name to place album in (script will find or create):",
    "folder_name": "YOUR_SMUGMUG_FOLDER_NAME", # Example: "Google Photos Transfer"

    "_comment_heic": "(Optional) Process HEIC files (default: false)? See README.",
    "process_heic": False
}

# --- Custom Exceptions ---
class SmugMugError(Exception):
    """Base exception for SmugMug module errors."""
    pass

class SmugMugAlbumFullError(SmugMugError):
    """Exception raised when the target SmugMug album is full (5000 items)."""
    pass
# --- End Custom Exceptions ---


class SmugMug:
    """Class encapsulating all SmugMug-related functionality."""

    SMUGMUG_API_BASE_URL = 'https://api.smugmug.com'
    SMUGMUG_UPLOAD_URL = 'https://upload.smugmug.com/'

    def __init__(self, config_file='smugmug_config.json'):
        """Initialize with the path to SmugMug configuration file."""
        self.config_file = config_file
        self.config = None  # Start as None, load explicitly in main
        self.auth_session = None
        # These will be populated during initialization/album check
        self.album_key = None
        self.album_api_uri = None
        self.album_name = None # Store the name used for creation/lookup
        self.folder_name = None # Store the target folder path
        self.username = None
        self.user_uri = None # Store the user's node URI
        # Don't load or authenticate here, do it explicitly in main.py

    def load_config(self):
        """
        Loads SmugMug API configuration.
        Returns True on success, False on JSON error.
        Raises FileNotFoundError if the file doesn't exist.
        """
        try:
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
                logger.info(f"Successfully loaded SmugMug config from {self.config_file}")
                return True
        except FileNotFoundError:
            # Let this exception propagate up to main.py
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from SmugMug config file: {self.config_file} - {e}")
            self.config = None
            return False
        except Exception as e:
            logger.error(f"Unexpected error loading SmugMug config {self.config_file}: {e}")
            self.config = None
            return False

    def generate_default_config(self):
        """Generates a default SmugMug config file with placeholders."""
        logger.warning(f"SmugMug config file '{self.config_file}' not found. Generating default.")
        try:
            with open(self.config_file, 'w') as f:
                # Use sort_keys=False to preserve the order defined in DEFAULT_SMUGMUG_CONFIG
                json.dump(DEFAULT_SMUGMUG_CONFIG, f, indent=2, sort_keys=False)
            logger.info(f"Default SmugMug config file created at '{self.config_file}'.")
            logger.info("Please edit this file to add your API Key and Secret, and configure your target album.")
            print(f"\nDefault SmugMug config file created at '{self.config_file}'.")
            print("--> Please edit this file to add your SmugMug API Key and Secret.")
            print("--> You also need to specify either 'album_name' OR 'album_key'/'album_api_uri'.")
            print("--> Then run the script again.")
            return True
        except IOError as e:
            logger.error(f"Error generating default SmugMug config file '{self.config_file}': {e}")
            print(f"Error: Could not write default SmugMug config file to '{self.config_file}'. Check permissions.")
            return False

    def save_config(self):
        """Saves the current SmugMug API configuration to its file."""
        if not self.config:
            logger.error("No SmugMug configuration loaded to save.")
            return False

        try:
            # Ensure comments and default values are present before saving
            temp_config = DEFAULT_SMUGMUG_CONFIG.copy() # Start with defaults
            temp_config.update(self.config) # Update with current values
            self.config = temp_config # Replace self.config with merged version

            with open(self.config_file, 'w') as f:
                 # Use sort_keys=False to preserve the order defined in DEFAULT_SMUGMUG_CONFIG
                json.dump(self.config, f, indent=2, sort_keys=False)
            logger.info(f"SmugMug configuration saved to {self.config_file}")
            return True
        except IOError as e:
            logger.error(f"Error saving SmugMug configuration to {self.config_file}: {e}")
            print(f"Error: Could not save updated SmugMug config to '{self.config_file}'. Check permissions.")
            return False
        except Exception as e:
             logger.error(f"Unexpected error saving SmugMug config: {e}")
             return False

    def obtain_oauth_tokens(self, api_key, api_secret):
        """Obtains SmugMug OAuth tokens using the OAuth 1.0a flow."""
        request_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getRequestToken'
        # Request full access and modify permissions
        authorize_url = 'https://secure.smugmug.com/services/oauth/1.0a/authorize?Access=Full&Permissions=Modify'
        access_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getAccessToken'

        # 'oob' (Out-Of-Band) is standard for desktop/CLI applications
        smugmug = OAuth1Session(api_key, client_secret=api_secret, callback_uri='oob')
        logger.info("Fetching request token from SmugMug...")
        try:
            # Step 1: Fetch request token
            fetch_response = smugmug.fetch_request_token(request_token_url)
            if fetch_response:
                # Step 2: Generate authorization URL
                auth_url = smugmug.authorization_url(authorize_url)
                print("-" * 60)
                print("SmugMug Authorization Required:")
                print("Please open the following URL in your browser to authorize this script:")
                print(auth_url)
                print("After authorizing, SmugMug will display a 6-digit code.")
                print("-" * 60)
                verifier = input("Enter the 6-digit verifier code here: ").strip()

                # Step 3: Fetch access token using the verifier
                logger.info("Fetching access token from SmugMug...")
                token_response = smugmug.fetch_access_token(access_token_url, verifier=verifier)
                if token_response:
                    oauth_token = token_response.get('oauth_token')
                    oauth_token_secret = token_response.get('oauth_token_secret')
                    if oauth_token and oauth_token_secret:
                         logger.info("Successfully obtained SmugMug access tokens.")
                         return oauth_token, oauth_token_secret
                    else:
                         logger.error("OAuth token response did not contain expected token/secret.")
                         return None, None
                else:
                    logger.error("Failed to fetch access token from SmugMug. Response was empty or invalid (maybe incorrect verifier code?).")
                    return None, None
            else:
                logger.error("Failed to fetch request token from SmugMug. Check API key/secret or network.")
                return None, None
        except ValueError as ve:
             logger.error(f"Error during SmugMug OAuth flow (potentially invalid verifier code?): {ve}")
             return None, None
        except Exception as e:
            logger.error(f"An error occurred during SmugMug OAuth flow: {e}", exc_info=True)
            return None, None

    def check_config_and_authenticate(self):
        """
        Checks for essential config keys, updates config if needed, performs OAuth,
        and verifies authentication. Returns True on success, False otherwise.
        """
        if not self.config:
            logger.error("SmugMug configuration is not loaded. Cannot authenticate.")
            return False

        # --- Define Placeholders (used for checking if values are defaults) ---
        placeholders = {
            "api_key": "YOUR_SMUGMUG_API_KEY",
            "api_secret": "YOUR_SMUGMUG_API_SECRET",
            "oauth_token": "YOUR_SMUGMUG_OAUTH_TOKEN",
            "oauth_token_secret": "YOUR_SMUGMUG_OAUTH_TOKEN_SECRET",
            "album_name": "YOUR_SMUGMUG_ALBUM_NAME",
            "album_key": "TARGET_SMUGMUG_ALBUM_KEY",
            "album_api_uri": "/api/v2/album/TARGET_SMUGMUG_ALBUM_KEY",
            "folder_name": "YOUR_SMUGMUG_FOLDER_NAME"
        }

        # --- Check Essential API Keys ---
        missing_keys = []
        config_updated = False
        for key in ["api_key", "api_secret"]:
            value = self.config.get(key)
            # Check if key is missing, empty string, or still the placeholder value
            if not value or value == placeholders.get(key):
                missing_keys.append(key)
                if key not in self.config: # Add missing key with placeholder
                    self.config[key] = placeholders.get(key)
                    # Add relevant comment if missing too
                    if key == "api_key" and "_comment_api" not in self.config:
                         self.config["_comment_api"] = DEFAULT_SMUGMUG_CONFIG["_comment_api"]
                    config_updated = True

        if missing_keys:
            logger.error(f"Missing or placeholder values in '{self.config_file}' for: {', '.join(missing_keys)}")
            logger.error("Please obtain these from SmugMug (https://api.smugmug.com/api/developer/apply) and update the config file.")
            print(f"\nError: Missing required SmugMug API credentials in '{self.config_file}': {', '.join(missing_keys)}")
            print("--> Please edit the file and add your credentials, then run again.")
            if config_updated:
                self.save_config() # Save the updated config with placeholders/comments
            return False

        api_key = self.config['api_key']
        api_secret = self.config['api_secret']

        # --- Check OAuth Tokens (and obtain if necessary) ---
        oauth_token = self.config.get('oauth_token')
        oauth_token_secret = self.config.get('oauth_token_secret')

        # Check if tokens are missing, empty, or still the placeholder
        if not oauth_token or not oauth_token_secret or oauth_token == placeholders['oauth_token']:
            logger.info("SmugMug OAuth tokens not found or are placeholders in config. Initiating authorization flow...")
            new_oauth_token, new_oauth_token_secret = self.obtain_oauth_tokens(api_key, api_secret)
            if new_oauth_token and new_oauth_token_secret:
                self.config['oauth_token'] = new_oauth_token
                self.config['oauth_token_secret'] = new_oauth_token_secret
                # Update local variables for immediate use
                oauth_token = new_oauth_token
                oauth_token_secret = new_oauth_token_secret
                # Save the newly obtained tokens to the config file
                if not self.save_config():
                     logger.error("Failed to save SmugMug config after obtaining OAuth tokens.")
                     print("\nWarning: Failed to save updated SmugMug tokens to config file. You may need to authorize again next time.")
                else:
                     logger.info("Successfully obtained and saved SmugMug OAuth tokens.")
                     config_updated = True # Mark as updated (though already saved)
            else:
                logger.error("Failed to obtain SmugMug OAuth tokens. Please check logs/API keys and try again.")
                print("\nError: Failed to obtain SmugMug authorization. Please ensure API key/secret are correct and try again.")
                return False
        else:
             logger.debug("Found existing SmugMug OAuth tokens in config.")

        # --- Create Auth Session ---
        try:
             self.auth_session = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token,
                                               resource_owner_secret=oauth_token_secret)
             logger.info("SmugMug OAuth session created.")
             # Verify authentication with a lightweight API call
             _, user_data = self.get_user_endpoint() # This implicitly checks authentication
             if not user_data:
                  # get_user_endpoint logs the specific error
                  raise Exception("Failed to verify SmugMug authentication via user endpoint.")
             logger.info(f"SmugMug authentication successful for user: {self.username}")

        except Exception as auth_err:
             logger.error(f"SmugMug authentication failed: {auth_err}", exc_info=True)
             # *** Add more detailed logging of the raw error ***
             logger.debug(f"Raw auth error details: {auth_err}") # Log raw error at debug level
             # Check if the error message indicates invalid token explicitly
             if "oauth_problem=token_rejected" in str(auth_err) or "Invalid OAuth signature" in str(auth_err) or "Invalid Token" in str(auth_err):
                 print("\nError: SmugMug authentication failed. Tokens might be invalid or expired.")
                 print(f"--> Attempting to clear potentially invalid tokens from '{self.config_file}'. Please run again to re-authorize.")
                 # Clear potentially invalid tokens
                 self.config['oauth_token'] = placeholders['oauth_token']
                 self.config['oauth_token_secret'] = placeholders['oauth_token_secret']
                 if not self.save_config(): # Save cleared tokens
                      logger.error(f"Failed to clear invalid tokens in {self.config_file}")
                 self.auth_session = None # Ensure session is cleared
                 return False
             else:
                 # For other errors, just report failure without clearing tokens
                 print(f"\nError: SmugMug authentication failed: {auth_err}")
                 return False


        # --- Check Album Configuration ---
        album_name_cfg = self.config.get('album_name')
        album_key_cfg = self.config.get('album_key')
        album_api_uri_cfg = self.config.get('album_api_uri')
        folder_name_cfg = self.config.get('folder_name')

        # Standardize: treat empty strings or placeholder values as None for logic checks
        album_name = album_name_cfg if album_name_cfg and album_name_cfg != placeholders['album_name'] else None
        album_key = album_key_cfg if album_key_cfg and album_key_cfg != placeholders['album_key'] else None
        album_api_uri = album_api_uri_cfg if album_api_uri_cfg and album_api_uri_cfg != placeholders['album_api_uri'] else None
        folder_name = folder_name_cfg if folder_name_cfg and folder_name_cfg != placeholders['folder_name'] else None

        # Derive URI from key if URI is missing/placeholder but key is valid
        if album_key and not album_api_uri:
            album_api_uri = f'/api/v2/album/{album_key}'
            logger.info(f"Derived album_api_uri from valid album_key: {album_api_uri}")
            # Update config if derived - mark for saving
            if self.config.get('album_api_uri') != album_api_uri:
                self.config['album_api_uri'] = album_api_uri
                config_updated = True

        has_valid_album_name = bool(album_name)
        has_valid_album_key_uri = bool(album_key) and bool(album_api_uri)

        # Check if at least one valid album specification method exists
        if not has_valid_album_name and not has_valid_album_key_uri:
            logger.error(f"Missing album configuration in '{self.config_file}'.")
            logger.error("Please specify either a valid 'album_name' OR both 'album_key' and 'album_api_uri'.")
            print(f"\nError: Missing SmugMug album configuration in '{self.config_file}'.")
            print("--> Please edit the file and provide either 'album_name' OR 'album_key' & 'album_api_uri'.")
            # Add missing placeholders if needed, mark for saving
            needs_save = False
            if "album_name" not in self.config:
                 self.config["album_name"] = placeholders["album_name"]
                 if "_comment_album_name" not in self.config:
                      self.config["_comment_album_name"] = DEFAULT_SMUGMUG_CONFIG["_comment_album_name"]
                 needs_save = True
            if "album_key" not in self.config:
                 self.config["album_key"] = placeholders["album_key"]
                 if "_comment_album_key" not in self.config:
                      self.config["_comment_album_key"] = DEFAULT_SMUGMUG_CONFIG["_comment_album_key"]
                 needs_save = True
            if "album_api_uri" not in self.config:
                 self.config["album_api_uri"] = placeholders["album_api_uri"]
                 needs_save = True
            # Add other missing optional keys/comments if desired (folder_name, process_heic)
            if "folder_name" not in self.config:
                 self.config["folder_name"] = placeholders["folder_name"]
                 if "_comment_folder" not in self.config:
                      self.config["_comment_folder"] = DEFAULT_SMUGMUG_CONFIG["_comment_folder"]
                 needs_save = True
            if "process_heic" not in self.config:
                 self.config["process_heic"] = DEFAULT_SMUGMUG_CONFIG["process_heic"]
                 if "_comment_heic" not in self.config:
                      self.config["_comment_heic"] = DEFAULT_SMUGMUG_CONFIG["_comment_heic"]
                 needs_save = True

            if needs_save:
                 self.save_config() # Save updated config with placeholders
            return False

        # If both methods are specified, prefer Key/URI but log a warning
        if has_valid_album_name and has_valid_album_key_uri:
            logger.warning("Both 'album_name' and 'album_key'/'album_api_uri' are specified in config.")
            logger.warning(f"Prioritizing 'album_key': {album_key}")
            # Clear album_name in the config to avoid ambiguity for get_or_create
            if self.config.get('album_name') != placeholders['album_name']:
                 self.config['album_name'] = placeholders['album_name']
                 config_updated = True

        # Store the final determined target info in the instance
        self.album_key = album_key
        self.album_api_uri = album_api_uri
        self.album_name = album_name # This will be None if Key/URI was prioritized
        self.folder_name = folder_name # Store folder name (can be None)

        # Save config if any derived values or corrections were made
        if config_updated:
             self.save_config()

        # If we reached here, authentication is verified and album config is present (though album existence is checked later)
        return True

    def _make_api_request(self, method, url, **kwargs):
        """Helper method to make authenticated API requests."""
        if not self.auth_session:
            logger.error("Cannot make SmugMug API request: Not authenticated.")
            return None, None

        full_url = self.SMUGMUG_API_BASE_URL + url if not url.startswith('http') else url
        headers = kwargs.pop('headers', {})
        headers['Accept'] = 'application/json' # Ensure we always get JSON back

        try:
            response = self.auth_session.request(method, full_url, headers=headers, **kwargs)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            # Check if response is valid JSON
            try:
                 data = response.json()
                 # Check SmugMug's 'stat' field if present
                 if isinstance(data, dict) and data.get('stat') == 'fail':
                      msg = data.get('message', 'Unknown SmugMug API error')
                      code = data.get('code', 'N/A')
                      logger.error(f"SmugMug API call failed ({method} {url}): Code {code} - {msg}")
                      return response, None # Return response for potential inspection, but data is None
                 return response, data
            except json.JSONDecodeError:
                 logger.error(f"Failed to decode JSON response from {method} {url}. Response text: {response.text[:200]}...")
                 return response, None # Return response, but indicate data decoding failure

        except requests.exceptions.RequestException as e:
            logger.error(f"SmugMug API request failed ({method} {url}): {e}", exc_info=True)
            return None, None
        except Exception as e:
            logger.error(f"Unexpected error during SmugMug API request ({method} {url}): {e}", exc_info=True)
            return None, None

    def get_user_endpoint(self):
        """Gets the authenticated user's information and node URI."""
        if self.user_uri: # Return cached if already fetched
             return self.user_uri, {"NickName": self.username}

        logger.info("Fetching SmugMug authenticated user info...")
        # !authuser is a shortcut for the logged-in user's node
        _, data = self._make_api_request('GET', '/api/v2!authuser?_expand=Node')
        if data and 'Response' in data and 'User' in data['Response']:
            user_info = data['Response']['User']
            # *** Add detailed logging of the response structure ***
            logger.debug(f"!authuser user_info received: {user_info}") # Log the received structure
            self.username = user_info.get('NickName', 'UnknownUser')
            # The expanded Node contains the User's root node URI needed for folder operations
            # --- Replacement Code Block ---
            # Get the URI of the user's node from the Uris section
            node_uri_info = user_info.get('Uris', {}).get('Node', {})
            user_node_uri_string = node_uri_info.get('Uri')

            if user_node_uri_string:
                # Successfully found the URI string (e.g., '/api/v2/node/vQvbk')
                self.user_uri = user_node_uri_string
                logger.info(f"Found User Node URI: {self.user_uri}")
                # Optional: You could also retrieve the expanded node details if needed:
                # expanded_node_details = data.get('Expansions', {}).get(self.user_uri)
                # if expanded_node_details:
                #     logger.debug(f"Expanded node details: {expanded_node_details}")
                return self.user_uri, user_info  # Return the URI string and user_info
            else:
                # If the Uris -> Node -> Uri path doesn't exist in the response
                logger.error("Could not find Node URI string within user_info['Uris']['Node']['Uri'].")
                return None, None
        # --- End of Replacement Code Block ---
        else:
            # Log the raw data if the structure is unexpected
            logger.error(f"Failed to get valid user information from SmugMug. Raw response data: {data}")
            return None, None

    def upload_media(self, album_api_uri, file_path, filename, mime_type):
        """
        Uploads a media file to the specified SmugMug album URI.
        Handles cleanup of the temporary file.
        Raises SmugMugAlbumFullError if the album limit is reached.
        """
        if not self.auth_session:
            logger.error(f"Cannot upload '{filename}': Not authenticated with SmugMug.")
            return False
        if not os.path.exists(file_path):
            logger.error(f"Cannot upload '{filename}': File not found at {file_path}")
            return False

        headers = {
            'X-Smug-ResponseType': 'JSON',
            'X-Smug-Version': 'v2',
            'X-Smug-AlbumUri': album_api_uri,
            'X-Smug-FileName': filename,
            'Content-Type': mime_type,
            'Content-Length': str(os.path.getsize(file_path)),
        }

        logger.info(f"Attempting upload for '{filename}' ({mime_type}, {headers['Content-Length']} bytes) to SmugMug album URI: {album_api_uri}")

        try:
            with open(file_path, 'rb') as f:
                response = self.auth_session.post(self.SMUGMUG_UPLOAD_URL, headers=headers, data=f)
                # Don't raise_for_status immediately, check SmugMug specific errors first

            # Attempt to parse JSON response regardless of HTTP status (SmugMug might return errors with 200 OK)
            try:
                response_data = response.json()
                logger.debug(f"SmugMug Upload Response JSON: {response_data}") # Log full response at debug

                # Check SmugMug specific status first
                if response_data.get('stat') == 'fail':
                    error_code = response_data.get('code')
                    error_message = response_data.get('message', 'Unknown SmugMug upload error')
                    logger.error(f"SmugMug upload failed for '{filename}': Code {error_code} - {error_message}")
                    # *** Check for Album Full error code (63) ***
                    if error_code == 63:
                        raise SmugMugAlbumFullError(error_message) # Raise specific exception
                    return False # Other SmugMug 'fail' status
                elif response_data.get('stat') == 'ok' and ('Image' in response_data or 'Video' in response_data): # Check for Image or Video key
                    # Check HTTP status only if SmugMug stat is 'ok'
                    response.raise_for_status() # Now check HTTP status for non-SmugMug errors
                    logger.info(f"Successfully uploaded '{filename}' to SmugMug.")
                    return True
                else:
                    # Unexpected structure in 'ok' response or missing stat
                    logger.error(f"Unexpected SmugMug upload response format for '{filename}': {response_data}")
                    return False

            except json.JSONDecodeError:
                # If JSON decoding fails, now check HTTP status
                response.raise_for_status() # Raise if it was an HTTP error without JSON body
                # If HTTP status was OK but JSON failed, it's an unexpected success response format
                logger.error(f"Failed to decode JSON response after uploading '{filename}'. Status: {response.status_code}, Text: {response.text[:200]}...")
                return False
            except SmugMugAlbumFullError: # Re-raise the specific error
                 raise
            except Exception as e: # Catch other potential errors during response processing
                 logger.error(f"Error processing SmugMug upload response for '{filename}': {e}", exc_info=True)
                 return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during SmugMug upload for '{filename}': {e}", exc_info=True)
            return False
        except SmugMugAlbumFullError: # Catch and re-raise
             # Logging is done in the except block in process_item_worker
             raise
        except Exception as e:
            logger.error(f"Unexpected error during SmugMug upload process for '{filename}': {e}", exc_info=True)
            return False
        finally:
            # Ensure temporary file is always deleted
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.debug(f"Cleaned up temporary file: {file_path}")
                except OSError as e:
                    logger.warning(f"Failed to remove temporary file {file_path}: {e}")

    def check_media_exists(self, album_key, filename, mime_type, file_hash=None):
        """
        Checks if media exists in the specified SmugMug album.
        Uses MD5 hash for images (if provided) or filename for videos.
        """
        if not self.auth_session:
            logger.error("Cannot check media existence: Not authenticated.")
            return False
        if not album_key:
             logger.error("Cannot check media existence: Album Key is missing.")
             return False

        is_video = mime_type.startswith('video/')
        check_method = "MD5 hash" if not is_video and file_hash else "filename"
        logger.debug(f"Checking SmugMug album {album_key} for '{filename}' via {check_method}...")

        # Construct the base album images/videos URI
        album_media_uri = f'/api/v2/album/{album_key}!images' # Check images endpoint first

        params = {'count': 100} # Fetch in batches
        if is_video:
            # For videos, filter by filename (case-insensitive search not directly supported, requires client-side check)
            # SmugMug search is limited, so we fetch batches and check locally
             params['_filter'] = 'FileName' # Fetch filenames
             params['_filteruri'] = album_media_uri # Ensure we stay within the album
             search_uri = '/api/v2/image!search' # Use search endpoint
        elif file_hash:
            # For images, filter directly by MD5 hash if available
            params['_filter'] = 'ArchivedMD5'
            params['_filtervalue'] = file_hash
            search_uri = album_media_uri # Search within the album's images
        else:
            # Image without hash (e.g., HEIC placeholder) - check by filename like video
            is_video = True # Treat like video for search logic
            params['_filter'] = 'FileName'
            params['_filteruri'] = album_media_uri
            search_uri = '/api/v2/image!search'

        next_page_start = 1
        while True:
            params['start'] = next_page_start
            _, data = self._make_api_request('GET', search_uri, params=params)

            if data and 'Response' in data and 'Image' in data['Response']:
                 media_list = data['Response']['Image']
                 if not media_list: # No more items found in this batch/page
                      break

                 for media in media_list:
                      if is_video:
                           smugmug_filename = media.get('FileName')
                           if smugmug_filename and smugmug_filename.lower() == filename.lower():
                                logger.info(f"Found existing media '{filename}' in SmugMug album {album_key} by filename.")
                                return True
                      else: # Image hash match (filter should have handled this, but double-check)
                           if media.get('ArchivedMD5') == file_hash:
                                logger.info(f"Found existing media '{filename}' in SmugMug album {album_key} by MD5 hash.")
                                return True

                 # Check pagination
                 paging = data['Response'].get('Pages')
                 if paging and paging.get('NextPage'):
                      next_page_uri = paging['NextPage']
                      # Extract start parameter for the next request
                      try:
                           # Basic parsing, might need adjustment based on actual URI format
                           start_param = next_page_uri.split('start=')[1].split('&')[0]
                           next_page_start = int(start_param)
                           logger.debug(f"Paginating SmugMug check, next start: {next_page_start}")
                      except (IndexError, ValueError) as parse_err:
                           logger.warning(f"Could not parse NextPage URI for pagination: {next_page_uri} - {parse_err}")
                           break # Stop pagination if URI is unparseable
                 else:
                      break # No more pages
            elif data and 'Response' in data and 'Image' not in data['Response']:
                 # Response received but no 'Image' key - means no matches found or empty album
                 logger.debug(f"No 'Image' key in SmugMug response for album {album_key}. Assuming not found.")
                 break
            else:
                 # API request failed or returned unexpected data
                 logger.warning(f"Failed to retrieve media list from SmugMug album {album_key} for duplicate check.")
                 return False # Treat API errors during check as potentially not found, safer to re-upload

        # If loop completes without finding a match
        logger.debug(f"Media '{filename}' not found in SmugMug album {album_key} via {check_method}.")
        return False

    # Replacement for get_or_create_folder in smugmug_module.py
    def get_or_create_folder(self, parent_node_uri, folder_name):
        """Finds or creates a folder within a parent node (User or Folder) by fetching children."""
        if not parent_node_uri or not folder_name:
            logger.error("Parent node URI and folder name are required.")
            return None

        logger.info(f"Checking for folder '{folder_name}' under node {parent_node_uri} by listing children...")

        # --- Use !children endpoint ---
        children_uri_base = f"{parent_node_uri}!children"
        found_folder_uri = None
        next_page_uri = children_uri_base  # Start with the initial children URI

        while next_page_uri:
            # Append count=100 for pagination efficiency if not already present in the base or next page URI
            # Ensure base_url isn't added repeatedly if next_page_uri is already absolute
            current_request_uri = next_page_uri
            if self.SMUGMUG_API_BASE_URL not in current_request_uri:
                current_request_uri = self.SMUGMUG_API_BASE_URL + current_request_uri

            # Add count parameter smartly
            separator = '&' if '?' in current_request_uri else '?'
            if 'count=' not in current_request_uri:
                current_request_uri += f"{separator}count=100"

            # Make the API request using the full URI
            _, data = self._make_api_request('GET', current_request_uri)  # Pass full URI directly

            if data and 'Response' in data and 'Node' in data['Response']:
                child_nodes = data['Response']['Node']
                for node in child_nodes:
                    # Case-insensitive check for folder name match
                    if node.get('Type') == 'Folder' and node.get('Name', '').lower() == folder_name.lower():
                        found_folder_uri = node.get('Uri')
                        logger.info(f"Found existing folder '{folder_name}' with Node URI: {found_folder_uri}")
                        # Ensure a valid URI was found before returning
                        if found_folder_uri:
                            return found_folder_uri  # Exit loop and return URI
                        else:
                            logger.warning(
                                f"Folder node found for '{folder_name}' but 'Uri' key is missing. Node data: {node}")
                            # Continue checking other nodes just in case, but this is unexpected

                # Check for pagination using the Pages dictionary from the Response
                paging = data['Response'].get('Pages')
                if paging and paging.get('NextPage'):
                    # SmugMug returns relative URIs in NextPage, handle potential double slashes
                    next_relative_uri = paging['NextPage']
                    # Construct absolute URL for next request to avoid issues with base URL in _make_api_request
                    next_page_uri = self.SMUGMUG_API_BASE_URL + next_relative_uri
                    logger.debug(f"Paginating children list for folder check, next URI: {next_page_uri}")
                else:
                    next_page_uri = None  # No more pages
            elif data and 'Response' in data and 'Node' not in data['Response']:
                logger.debug(
                    f"No 'Node' key in children response for {parent_node_uri}. No children found or empty page.")
                next_page_uri = None  # No children or empty page
            else:
                logger.warning(f"Failed to retrieve children list from {parent_node_uri} for folder check.")
                next_page_uri = None  # Stop trying if API call fails
                return None  # Treat API error as potentially fatal for this operation

        # --- Folder Not Found by Listing Children ---
        if not found_folder_uri:
            logger.info(f"Folder '{folder_name}' not found by listing children. Attempting to create...")
            # Generate a URL-safe name
            url_name_base = ''.join(c for c in folder_name if c.isalnum() or c in (' ', '-')).strip().title().replace(
                ' ', '')
            # Ensure UrlName is not empty and has a fallback
            url_name = url_name_base[
                       :50] if url_name_base else f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"
            if not url_name: url_name = f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"  # Double check for safety
            # Ensure UrlName starts with an uppercase letter if possible
            if url_name and url_name[0].islower(): url_name = url_name[0].upper() + url_name[1:]

            create_payload = {
                'Name': folder_name,
                'UrlName': url_name,
                'Type': 'Folder',  # Specify type when creating via parent node
                'Privacy': 'Private'  # Default privacy
            }
            # The POST request still targets the parent node's !children endpoint
            create_url = f"{parent_node_uri}!children"
            post_response, create_data = self._make_api_request('POST', create_url, json=create_payload)

            if create_data and 'Response' in create_data and 'Node' in create_data['Response']:
                new_folder_uri = create_data['Response']['Node'].get('Uri')  # Use .get() for safety
                if new_folder_uri:
                    logger.info(f"Folder '{folder_name}' created successfully with Node URI: {new_folder_uri}")
                    return new_folder_uri
                else:
                    logger.error(
                        f"Folder '{folder_name}' created, but response missing Node 'Uri'. Response: {create_data}")
                    return None
            else:
                # Log specific SmugMug error if available from the response object
                smugmug_error_details = ""
                if post_response is not None and 400 <= post_response.status_code < 600:
                    try:
                        error_json = post_response.json()
                        smugmug_error_details = f" (Code: {error_json.get('code', 'N/A')}, Message: {error_json.get('message', 'N/A')})"
                    except json.JSONDecodeError:
                        smugmug_error_details = f" (Status Code: {post_response.status_code}, Non-JSON Response: {post_response.text[:100]})"
                    except Exception:  # Catch any other error during parsing
                        smugmug_error_details = f" (Status Code: {post_response.status_code}, Error parsing response)"

                logger.error(f"Failed to create folder '{folder_name}' under {parent_node_uri}.{smugmug_error_details}")
                return None

        # Fallback if something unexpected happened (e.g., found_folder_uri was None but creation wasn't attempted)
        return None


    def get_or_create_album_in_path(self, album_name, folder_path_str):
        """
        Finds or creates an album, handling nested folders specified in folder_path_str.
        Updates self.album_key and self.album_api_uri on success.
        """
        # Get user's root node URI first
        user_node_uri, _ = self.get_user_endpoint()
        if not user_node_uri:
            logger.critical("Cannot proceed without User Node URI.")
            return False

        parent_node_uri = user_node_uri # Start search/creation from user root

        # Handle folder path if provided
        if folder_path_str:
             folder_names = [name.strip() for name in folder_path_str.split('/') if name.strip()]
             logger.info(f"Ensuring folder path exists: {'/'.join(folder_names)}")
             for current_folder_name in folder_names:
                  folder_uri = self.get_or_create_folder(parent_node_uri, current_folder_name)
                  if not folder_uri:
                       logger.error(f"Failed to find or create intermediate folder '{current_folder_name}'. Aborting album creation.")
                       return False
                  parent_node_uri = folder_uri # Set the new parent for the next iteration/album creation
             logger.info(f"Ensured folder path '{folder_path_str}' exists. Final parent node URI: {parent_node_uri}")
        else:
             logger.info("No folder path specified, targeting user root node.")

        # Now find or create the album within the final parent_node_uri
        logger.info(f"Checking for album '{album_name}' under node {parent_node_uri}...")
        # Search for the album by name within the parent node
        search_params = {
            'Scope': parent_node_uri,
            'Type': 'Album',
            'Text': album_name,
             '_filter': ['Name'], # Filter by Name field
             '_filterValue': [album_name] # Value must match Name exactly
        }
        _, search_data = self._make_api_request('GET', '/api/v2/node!search', params=search_params) # Use node!search

        if search_data and 'Response' in search_data and 'Node' in search_data['Response']:
             # Found existing node(s) - Check if it's the exact album we want
             for node in search_data['Response']['Node']:
                  if node.get('Type') == 'Album' and node.get('Name') == album_name:
                       album_uris = node.get('Uris', {})
                       self.album_api_uri = album_uris.get('Album', {}).get('Uri')
                       # Extract key from URI
                       self.album_key = self.album_api_uri.split('/')[-1] if self.album_api_uri else None
                       if self.album_key and self.album_api_uri:
                            logger.info(f"Found existing album '{album_name}' with Key: {self.album_key}, URI: {self.album_api_uri}")
                            self.album_name = album_name # Store the found name
                            return True # Album found
                       else:
                            logger.warning(f"Found album node for '{album_name}' but missing Key/URI.")

        # Album not found, create it
        logger.info(f"Album '{album_name}' not found. Attempting to create...")
        # Generate URL name
        url_name_base = ''.join(c for c in album_name if c.isalnum() or c in (' ', '-')).strip().title().replace(' ', '')
        url_name = url_name_base[:50] if url_name_base else f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}"
        if not url_name: url_name = f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}"
        if url_name and not url_name[0].isupper(): url_name = url_name[0].upper() + url_name[1:]

        create_payload = {
            'Name': album_name,
            'UrlName': url_name,
            'Type': 'Album', # Specify type when creating via parent node
            'Privacy': 'Private' # Default privacy
        }
        # POST to the parent node's !children endpoint
        create_url = f"{parent_node_uri}!children"
        _, create_data = self._make_api_request('POST', create_url, json=create_payload)

        if create_data and 'Response' in create_data and 'Node' in create_data['Response']:
             new_node_data = create_data['Response']['Node']
             album_uris = new_node_data.get('Uris', {})
             self.album_api_uri = album_uris.get('Album', {}).get('Uri')
             # Extract key from URI
             self.album_key = self.album_api_uri.split('/')[-1] if self.album_api_uri else None

             if self.album_api_uri and self.album_key:
                  logger.info(f"Album '{album_name}' created successfully with Key: {self.album_key}, URI: {self.album_api_uri}")
                  self.album_name = album_name # Store the created name
                  return True # Album created
             else:
                  logger.error(f"Album '{album_name}' created, but response missing Key or URI.")
                  return False
        else:
             logger.error(f"Failed to create album '{album_name}' under {parent_node_uri}.")
             return False


    @staticmethod
    def calculate_file_hash(file_path, hash_algorithm='md5'):
        """Calculates the hash of a file using the specified algorithm (default: md5)."""
        if hash_algorithm.lower() == 'md5':
            hasher = hashlib.md5()
        elif hash_algorithm.lower() == 'sha256':
            # sha256 might be useful if SmugMug supports it later
            hasher = hashlib.sha256()
        else:
            logger.error(f"Unsupported hash algorithm: {hash_algorithm}")
            return None

        try:
            with open(file_path, 'rb') as afile:
                # Read in chunks to handle large files efficiently
                buf_size = 65536 # 64KB chunks
                while True:
                    data = afile.read(buf_size)
                    if not data:
                        break
                    hasher.update(data)
            return hasher.hexdigest()
        except FileNotFoundError:
            logger.error(f"File not found for hashing: {file_path}")
            return None
        except Exception as e:
            logger.error(f"Error calculating {hash_algorithm.upper()} hash for {file_path}: {e}", exc_info=True)
            return None
