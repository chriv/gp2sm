# SmugMug Module
#
# This module encapsulates all SmugMug-related functionality for the Google Photos to SmugMug Transfer Script.
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

class SmugMug:
    """Class encapsulating all SmugMug-related functionality."""

    def __init__(self, config_file='smugmug_config.json'):
        """Initialize with the path to SmugMug configuration file."""
        self.config_file = config_file
        self.config = None  # Start as None, load explicitly in main
        self.auth_session = None
        self.album_key = None
        self.album_api_uri = None
        self.album_name = None
        self.folder_name = None
        self.username = None
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
                if "_comment_album_name" not in self.config: self.config["_comment_album_name"] = DEFAULT_SMUGMUG_CONFIG["_comment_album_name"]
                needs_save = True
            if "album_key" not in self.config:
                self.config["album_key"] = placeholders["album_key"]
                if "_comment_album_key" not in self.config: self.config["_comment_album_key"] = DEFAULT_SMUGMUG_CONFIG["_comment_album_key"]
                needs_save = True
            if "album_api_uri" not in self.config:
                 self.config["album_api_uri"] = placeholders["album_api_uri"]
                 needs_save = True
            # Add other missing optional keys/comments if desired (folder_name, process_heic)
            if "folder_name" not in self.config:
                 self.config["folder_name"] = placeholders["folder_name"]
                 if "_comment_folder" not in self.config: self.config["_comment_folder"] = DEFAULT_SMUGMUG_CONFIG["_comment_folder"]
                 needs_save = True
            if "process_heic" not in self.config:
                 self.config["process_heic"] = DEFAULT_SMUGMUG_CONFIG["process_heic"]
                 if "_comment_heic" not in self.config: self.config["_comment_heic"] = DEFAULT_SMUGMUG_CONFIG["_comment_heic"]
                 needs_save = True

            if needs_save:
                self.save_config()
            return False

        # --- Set internal attributes based on valid config (Priority: album_name > album_key/uri) ---
        if has_valid_album_name:
            self.album_name = album_name
            self.album_key = None
            self.album_api_uri = None
            logger.info(f"Using SmugMug album name from config: '{self.album_name}'")
        elif has_valid_album_key_uri:
            # Use the potentially derived URI if name wasn't valid
            self.album_name = None # Clear name if using key/uri
            self.album_key = album_key
            self.album_api_uri = album_api_uri
            logger.info(f"Using SmugMug album key from config: '{self.album_key}' (URI: {self.album_api_uri})")
        else:
             # Should be caught above, but as a failsafe:
             logger.critical("Critical internal error: No valid SmugMug album config found.")
             return False

        self.folder_name = folder_name # Set folder name attribute
        if self.folder_name:
             logger.info(f"Using SmugMug folder name from config: '{self.folder_name}'")

        # --- Check process_heic (add if missing) ---
        if "process_heic" not in self.config:
            logger.info("Adding missing 'process_heic' setting (default: false) to config.")
            self.config["process_heic"] = DEFAULT_SMUGMUG_CONFIG["process_heic"]
            if "_comment_heic" not in self.config: self.config["_comment_heic"] = DEFAULT_SMUGMUG_CONFIG["_comment_heic"]
            config_updated = True # Mark for saving if not already marked

        # Final save if any updates were made (like deriving URI or adding process_heic)
        if config_updated:
              if not self.save_config():
                   logger.warning("Failed to save SmugMug config after adding missing/derived keys.")

        return True # All checks passed, authenticated


    def check_media_exists(self, album_key, filename, mime_type, file_hash=None):
        """
        Checks if a media item exists in the specified SmugMug album.
        - For images (mime_type starting with 'image/'), checks by MD5 hash.
        - For videos (mime_type starting with 'video/'), checks by filename.
        Returns True if found, False otherwise.
        """
        if not self.auth_session:
            logger.warning("SmugMug authentication not set up. Cannot check for media existence.")
            return False

        # Use the instance's confirmed album_key if the passed one is None
        if not album_key:
            album_key = self.album_key
            if not album_key:
                logger.warning("No SmugMug Album Key available. Cannot check for media existence.")
                return False

        is_video = mime_type.startswith('video/')
        # MD5 is required for images per SmugMug API for existence checks
        if not is_video and not file_hash:
            logger.warning(f"Image file '{filename}' requires an MD5 hash for existence check, but none was provided.")
            return False  # Cannot check image without hash

        # Construct the API URL to list images in the album
        # Need to fetch the album details first to get the AlbumImages URI
        album_details_url = f"https://api.smugmug.com/api/v2/album/{album_key}"
        headers = {'Accept': 'application/json'}

        try:
            # First, get album details to find the Images URI
            logger.debug(f"Fetching album details from: {album_details_url}")
            album_response = self.auth_session.get(album_details_url, headers=headers)
            album_response.raise_for_status()  # Raise for HTTP errors
            album_data = album_response.json()

            # Navigate through the response to find the AlbumImages URI
            images_uri = album_data.get('Response', {}).get('Album', {}).get('Uris', {}).get('AlbumImages', {}).get('Uri')
            if not images_uri:
                 logger.error(f"Could not find AlbumImages URI for SmugMug album key {album_key}. Cannot check existence.")
                 logger.debug(f"Album details response: {json.dumps(album_data, indent=2)}")
                 return False

            # Construct base URL for listing images, add count for pagination efficiency
            images_list_url = f"https://api.smugmug.com{images_uri}?count=100"
            next_page_url = images_list_url # Start with the first page

            logger.debug(f"Starting media existence check in album {album_key} using URI: {images_uri}")

            # Now, iterate through the album images using the found URI
            page_count = 0
            while next_page_url:
                page_count += 1
                logger.debug(f"Checking SmugMug images page {page_count}: {next_page_url}")
                response = self.auth_session.get(next_page_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                items_in_response = data.get('Response', {}).get('AlbumImage', []) # Ensure it's a list
                if items_in_response:
                    for item in items_in_response:
                        # Ensure item is a dictionary before accessing keys
                        if not isinstance(item, dict):
                             logger.warning(f"Found non-dictionary item in AlbumImage list: {item}")
                             continue

                        item_filename = item.get('FileName')

                        # Check based on media type
                        if is_video:
                            # For videos, compare filenames (case-insensitive)
                            if item_filename and filename and filename.lower() == item_filename.lower():
                                logger.info(f"Video '{filename}' found on SmugMug by filename match.")
                                return True
                        else:
                            # For images, compare MD5 hashes (case-insensitive)
                            smugmug_md5 = item.get('ArchivedMD5') # This field stores the MD5 hash
                            if file_hash and smugmug_md5 and file_hash.lower() == smugmug_md5.lower():
                                logger.info(f"Image '{filename}' found on SmugMug with matching MD5 hash: {file_hash}")
                                return True # Exact match found

                # Pagination logic
                pages_info = data.get('Response', {}).get('Pages')
                if pages_info and 'NextPage' in pages_info and pages_info['NextPage']:
                    # Construct the full URL for the next page
                    next_page_uri = pages_info['NextPage']
                    if not next_page_uri.startswith("http"): # Ensure full URL
                         next_page_url = f"https://api.smugmug.com{next_page_uri}"
                    else:
                         next_page_url = next_page_uri
                    # Add count parameter to next page URL as well if not already present
                    if "?count=" not in next_page_url and "&count=" not in next_page_url:
                        separator = "&" if "?" in next_page_url else "?"
                        next_page_url += f"{separator}count=100"
                else:
                    next_page_url = None # No more pages

            # If the loop finishes without finding the item, it doesn't exist
            logger.debug(f"Media '{filename}' not found in SmugMug album {album_key} after checking all pages.")
            return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Error checking for media existence on SmugMug (filename: {filename}): {e}", exc_info=True)
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                try:
                    logger.error(f"SmugMug API Response Text: {e.response.text}")
                except Exception:
                     logger.error("Could not decode SmugMug API response text.")
            # Consider returning False cautiously - allows retry/upload attempt
            return False
        except Exception as e:
            logger.error(f"An unexpected error occurred during SmugMug existence check for '{filename}': {e}", exc_info=True)
            return False # Assume it doesn't exist on unexpected error

    def upload_media(self, album_api_uri, file_path, filename, mime_type):
        """
        Upload media file to specified Album API URI.
        Returns True if successful, False otherwise. Cleans up temp file on success/failure.
        """
        if not self.auth_session:
            logger.error("Not authenticated with SmugMug. Cannot upload media.")
            return False

        # Use instance's confirmed album_api_uri if passed one is None
        if not album_api_uri:
            album_api_uri = self.album_api_uri
            if not album_api_uri:
                logger.error("No SmugMug Album API URI available. Cannot upload media.")
                return False

        if not file_path or not os.path.exists(file_path):
            logger.error(f"Upload failed: File not found at {file_path}")
            return False

        upload_successful = False
        try:
            # Calculate MD5 hash for the uploaded file content as required by SmugMug
            file_md5 = self.calculate_file_hash(file_path, hash_algorithm='md5')
            if not file_md5:
                logger.error(f"Failed to calculate MD5 hash for {filename}. Cannot upload.")
                # Still need to clean up the temp file in finally block
                return False

            # Read file content for upload
            with open(file_path, 'rb') as media_file:
                media_data = media_file.read()

            # Define headers using instance attributes where possible
            headers = {
                'Accept': 'application/json',
                'Content-Length': str(len(media_data)),
                'Content-MD5': file_md5,
                'Content-Type': mime_type,
                'X-Smug-AlbumUri': album_api_uri, # Use the confirmed URI
                'X-Smug-FileName': filename,
                'X-Smug-ResponseType': 'JSON',
                'X-Smug-Version': 'v2',
            }

            logger.info(f"Attempting upload for '{filename}' ({mime_type}, {len(media_data)} bytes) to SmugMug album URI: {album_api_uri}")
            logger.debug(f"Upload Headers: {headers}")

            # The upload URL is fixed: https://upload.smugmug.com/
            upload_url = 'https://upload.smugmug.com/'
            response = self.auth_session.post(upload_url, headers=headers, data=media_data, timeout=300) # Add timeout

            logger.debug(f"Upload response status code: {response.status_code}")
            logger.debug(f"Upload response text: {response.text[:500]}...") # Log first 500 chars

            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            upload_data = response.json()

            # Check the response structure for success indication
            # Successful upload should return stat='ok' and contain an 'Image' or 'Video' object
            if upload_data.get('stat') == 'ok' and ('Image' in upload_data or 'Video' in upload_data):
                media_info = upload_data.get('Image') or upload_data.get('Video') # Get whichever is present
                status_url = media_info.get('StatusUri') if media_info else 'N/A' # Use StatusUri if available
                final_url = media_info.get('WebUri') if media_info else 'N/A' # Use WebUri if available

                logger.info(f"Successfully initiated upload for '{filename}'. SmugMug Status URI: {status_url}, Final URL (approx): {final_url}")
                upload_successful = True # Mark as successful for finally block logic
                return True
            else:
                logger.error(f"Failed to upload '{filename}' to SmugMug. Unexpected response format or status.")
                logger.error(f"SmugMug Upload Response: {json.dumps(upload_data, indent=2)}")
                return False

        except requests.exceptions.HTTPError as http_err:
             logger.error(f"HTTP Error uploading {filename} to SmugMug: {http_err}", exc_info=True)
             if http_err.response is not None:
                  logger.error(f"HTTP Status Code: {http_err.response.status_code}")
                  try:
                       logger.error(f"SmugMug API Response Text: {http_err.response.text}")
                  except Exception:
                       logger.error("Could not decode SmugMug API response text.")
             return False
        except requests.exceptions.RequestException as req_err:
            logger.error(f"Request Error uploading {filename} to SmugMug: {req_err}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"An unexpected error occurred during upload of '{filename}': {e}", exc_info=True)
            return False
        finally:
            # Clean up the temporary file ONLY if it exists AND upload was attempted
            # Avoid removing if it never existed or hash calc failed earlier
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.debug(f"Removed temporary upload file: {file_path}")
                except OSError as e:
                    logger.warning(f"Could not remove temporary file {file_path} after upload attempt: {e}")

    def get_user_endpoint(self):
        """
        Gets the authenticated user's details from the SmugMug API.
        Returns the username and user data dict if successful, None, None otherwise.
        Sets self.username on success.
        """
        if not self.auth_session:
            logger.error("Not authenticated with SmugMug. Cannot get user endpoint.")
            return None, None

        try:
            # Get the user data from the SmugMug API v2 entry point !authuser
            user_url = "https://api.smugmug.com/api/v2!authuser"
            headers = {'Accept': 'application/json'}
            logger.debug(f"Getting user endpoint from: {user_url}")
            response = self.auth_session.get(user_url, headers=headers)
            response.raise_for_status() # Check for HTTP errors
            user_data = response.json()

            # Extract the username (NickName) from the user data
            user_info = user_data.get('Response', {}).get('User', {})
            username = user_info.get('NickName')

            if not username:
                logger.error("Could not find username (NickName) in SmugMug API response.")
                logger.debug(f"User endpoint response: {json.dumps(user_data, indent=2)}")
                return None, None

            logger.debug(f"Found SmugMug username: {username}")
            self.username = username # Store the username

            return username, user_data # Return username and full response dict

        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting user endpoint from SmugMug: {e}", exc_info=True)
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                 try:
                    logger.error(f"SmugMug API Response Text: {e.response.text}")
                 except Exception:
                    logger.error("Could not decode SmugMug API response text.")
            return None, None
        except Exception as e:
            logger.error(f"An unexpected error occurred while getting user endpoint from SmugMug: {e}", exc_info=True)
            return None, None

    def _get_node_uri(self, user_data):
         """Helper to extract the root Node URI from user data."""
         if not user_data: return None
         node_uri = user_data.get('Response', {}).get('User', {}).get('Uris', {}).get('Node', {}).get('Uri')
         if not node_uri:
              logger.error("Could not find root Node URI in user data.")
              logging.debug(f"User data for Node URI check: {json.dumps(user_data, indent=2)}")
         return node_uri

    # --- CORRECTED list_albums ---
    def list_albums(self, node_uri=None):
        """
        Lists all albums under a specific node URI (using !children) or the user's root node (!albums).
        Returns a list of album dictionaries if successful, empty list otherwise.
        """
        if not self.auth_session:
            logger.error("Not authenticated with SmugMug. Cannot list albums.")
            return []

        headers = {'Accept': 'application/json'}
        all_albums = []
        page_count = 0
        target_uri = "" # The URI we will actually query
        is_listing_children = False # Flag to indicate if we need to filter results

        if node_uri:
            # If a specific node_uri is provided, list its children and filter for albums
            logger.debug(f"Listing children under provided node {node_uri} to find albums.")
            # Ensure count is always added correctly
            separator = "&" if "?" in node_uri else "?"
            target_uri = f"https://api.smugmug.com{node_uri}!children{separator}count=100"
            is_listing_children = True
        else:
            # If no node_uri, list albums from the user's root using !albums endpoint
            if not self.username: # Ensure username is available
                _, user_data = self.get_user_endpoint()
                if not user_data: return []
            if not self.username: # Still no username? Error out.
                logger.error("Cannot list albums: Failed to get username.")
                return []

            target_uri = f"https://api.smugmug.com/api/v2/user/{self.username}!albums?count=100"
            logger.debug(f"Listing albums under user root using {target_uri}")
            is_listing_children = False

        next_page_url = target_uri # Initialize pagination URL

        while next_page_url:
            page_count += 1
            logging.debug(f"Fetching SmugMug data page {page_count} from: {next_page_url}")
            try:
                response = self.auth_session.get(next_page_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                items_in_response = []
                if is_listing_children:
                    # Filter nodes of type 'Album' when listing children
                    nodes = data.get('Response', {}).get('Node', [])
                    # Extract album details if the node represents an album
                    for node in nodes:
                         if isinstance(node, dict) and node.get('Type') == 'Album':
                              items_in_response.append(node)
                    logger.debug(f"Found {len(items_in_response)} nodes of type Album on page {page_count}.")
                else:
                    # Directly use the 'Album' list when using !albums endpoint
                    items_in_response = data.get('Response', {}).get('Album', [])
                    logger.debug(f"Found {len(items_in_response)} albums directly on page {page_count}.")


                if items_in_response:
                    all_albums.extend(items_in_response)
                    logging.debug(f"Total albums accumulated: {len(all_albums)}")

                # Pagination
                pages_info = data.get('Response', {}).get('Pages')
                if pages_info and 'NextPage' in pages_info and pages_info['NextPage']:
                    next_page_uri = pages_info['NextPage']
                    # Ensure full URL and add count parameter
                    if not next_page_uri.startswith("http"):
                         next_page_url = f"https://api.smugmug.com{next_page_uri}"
                    else:
                         next_page_url = next_page_uri
                    if "?count=" not in next_page_url and "&count=" not in next_page_url:
                         separator = "&" if "?" in next_page_url else "?"
                         next_page_url += f"{separator}count=100"
                else:
                    next_page_url = None # No more pages

            except requests.exceptions.RequestException as e:
                logger.error(f"Error listing from SmugMug URI {next_page_url}: {e}", exc_info=True)
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    try:
                        logger.error(f"SmugMug API Response Text: {e.response.text}")
                    except Exception:
                        logger.error("Could not decode SmugMug API response text.")
                return [] # Return empty on error
            except Exception as e:
                logger.error(f"An unexpected error occurred while listing from SmugMug URI {next_page_url}: {e}", exc_info=True)
                return []

        logger.info(f"Found total of {len(all_albums)} albums matching criteria.")
        return all_albums
    # --- End CORRECTED list_albums ---

    def list_folders(self, node_uri=None):
        """
        Lists folders under a specific node URI or the user's root node.
        Returns a list of folder dictionaries if successful, empty list otherwise.
        """
        if not self.auth_session:
            logger.error("Not authenticated with SmugMug. Cannot list folders.")
            return []

        if not node_uri:
            # Get the user's root node URI if not provided
            _, user_data = self.get_user_endpoint()
            if not user_data: return []
            node_uri = self._get_node_uri(user_data)
            if not node_uri:
                 logger.error("Cannot list folders: Failed to get root node URI.")
                 return []
            logging.debug(f"Listing folders under root node: {node_uri}")
        else:
            logging.debug(f"Listing folders under provided node: {node_uri}")

        # Get the children of the specified node
        # Ensure count=100 is added correctly
        separator = "&" if "?" in node_uri else "?"
        children_url = f"https://api.smugmug.com{node_uri}!children{separator}count=100"

        headers = {'Accept': 'application/json'}
        all_folders = []
        page_count = 0

        while children_url:
             page_count += 1
             logger.debug(f"Fetching node children page {page_count} from: {children_url}")
             try:
                  response = self.auth_session.get(children_url, headers=headers)
                  response.raise_for_status()
                  data = response.json()

                  nodes_in_response = data.get('Response', {}).get('Node', []) # List of child nodes
                  if nodes_in_response:
                      # Filter for folders
                      folders_on_page = [node for node in nodes_in_response if isinstance(node, dict) and node.get('Type') == 'Folder']
                      if folders_on_page:
                           all_folders.extend(folders_on_page)
                           logger.debug(f"Found {len(folders_on_page)} folders on page {page_count}. Total found: {len(all_folders)}")

                  # Pagination
                  pages_info = data.get('Response', {}).get('Pages')
                  if pages_info and 'NextPage' in pages_info and pages_info['NextPage']:
                      next_page_uri = pages_info['NextPage']
                      if not next_page_uri.startswith("http"):
                          children_url = f"https://api.smugmug.com{next_page_uri}"
                      else:
                           children_url = next_page_uri
                      # Add count parameter if not already there
                      if "?count=" not in children_url and "&count=" not in children_url:
                           separator = "&" if "?" in children_url else "?"
                           children_url += f"{separator}count=100"
                  else:
                       children_url = None # No more pages

             except requests.exceptions.RequestException as e:
                  logger.error(f"Error listing children from SmugMug node {node_uri}: {e}", exc_info=True)
                  if hasattr(e, 'response') and hasattr(e.response, 'text'):
                      try:
                         logger.error(f"SmugMug API Response Text: {e.response.text}")
                      except Exception:
                         logger.error("Could not decode SmugMug API response text.")
                  return [] # Return empty on error
             except Exception as e:
                  logger.error(f"An unexpected error occurred while listing children from node {node_uri}: {e}", exc_info=True)
                  return []

        logger.info(f"Found total of {len(all_folders)} folders under node {node_uri}.")
        return all_folders

    def select_album_by_name(self, album_name, parent_node_uri=None):
        """
        Selects an album by name under a specific parent node URI (or root node).
        Returns True if found and sets instance attributes, False otherwise.
        """
        if not album_name:
            logger.warning("Cannot select album: No album name provided.")
            return False

        logger.debug(f"Attempting to select album '{album_name}' under node URI: {parent_node_uri or 'root'}")
        # Use the corrected list_albums which handles parent_node_uri correctly
        albums = self.list_albums(node_uri=parent_node_uri)
        if not albums:
            # Already logged in list_albums if error occurred or no albums found
            logging.debug(f"No albums found under node {parent_node_uri or 'root'} to select from.")
            return False

        # Find the album with the matching name (case-insensitive)
        for album_node in albums: # Now iterating through nodes of Type 'Album'
             # Ensure album_node is a dictionary
             if not isinstance(album_node, dict):
                  logger.warning(f"Found non-dictionary item in albums list: {album_node}")
                  continue

             current_album_name = album_node.get('Name')
             if current_album_name and album_name.lower() == current_album_name.lower():
                # Extract key/uri from the node's Uris structure
                album_uri_info = album_node.get('Uris', {}).get('Album')
                if not album_uri_info or not isinstance(album_uri_info, dict):
                     logger.error(f"Album node '{current_album_name}' found, but missing valid 'Album' URI structure.")
                     logger.debug(f"Node data: {json.dumps(album_node, indent=2)}")
                     continue # Skip this malformed node

                album_uri = album_uri_info.get('Uri')
                # Parse key from URI if available
                album_key = album_uri.split('/')[-1] if album_uri else None

                if not album_key or not album_uri:
                    logger.error(f"Album node '{current_album_name}' found, but missing AlbumKey ('{album_key}') or Album URI ('{album_uri}') in URI data.")
                    logger.debug(f"Node data: {json.dumps(album_node, indent=2)}")
                    return False # Treat as failure if key/uri missing

                # Set instance variables on successful selection
                self.album_key = album_key
                self.album_api_uri = album_uri
                self.album_name = current_album_name # Store the exact name found
                logger.info(f"Selected existing album '{current_album_name}' with key {self.album_key} and URI {self.album_api_uri}")
                return True

        logger.debug(f"Album '{album_name}' not found under node URI: {parent_node_uri or 'root'}")
        return False

    def select_folder_by_name(self, folder_name, parent_node_uri=None):
        """
        Selects a folder by name under a specific parent node URI (or root node).
        Returns the folder's Node URI if found, None otherwise.
        """
        if not folder_name:
             logger.warning("Cannot select folder: No folder name provided.")
             return None

        logger.debug(f"Attempting to select folder '{folder_name}' under node URI: {parent_node_uri or 'root'}")
        folders = self.list_folders(node_uri=parent_node_uri) # List folders under the specific node
        if not folders:
             # Already logged in list_folders if error occurred or no folders found
            logging.debug(f"No folders found under node {parent_node_uri or 'root'} to select from.")
            return None

        # Find the folder with the matching name (case-insensitive)
        for folder in folders:
             # Ensure folder is a dictionary
             if not isinstance(folder, dict):
                  logger.warning(f"Found non-dictionary item in folders list: {folder}")
                  continue

             current_folder_name = folder.get('Name')
             if current_folder_name and folder_name.lower() == current_folder_name.lower():
                # We need the *Node* URI for the folder to create things under it
                folder_node_uri = folder.get('Uri') # The 'Uri' key on a folder node *is* its node URI

                if not folder_node_uri:
                    logger.error(f"Folder '{current_folder_name}' found, but missing Node Uri in data: {json.dumps(folder, indent=2)}")
                    return None

                logger.info(f"Selected existing folder '{current_folder_name}' with Node URI {folder_node_uri}")
                return folder_node_uri # Return the Node URI

        logger.debug(f"Folder '{folder_name}' not found under node URI: {parent_node_uri or 'root'}")
        return None

    # --- CORRECTED create_album ---
    def create_album(self, album_name, parent_node_uri=None):
        """
        Creates a new album under the specified parent node URI (or root node).
        Returns the album key and album URI if successful, None, None otherwise.
        Sets instance attributes and saves config on success. Includes attribute validation.
        """
        if not self.auth_session:
            logger.error("Not authenticated with SmugMug. Cannot create album.")
            return None, None
        if not album_name:
            logger.error("Album name not provided. Cannot create album.")
            return None, None

        if not parent_node_uri:
            # Default to creating under the user's root node
            _, user_data = self.get_user_endpoint()
            if not user_data: return None, None # Failed to get user data
            parent_node_uri = self._get_node_uri(user_data)
            if not parent_node_uri: return None, None # Failed to get root node URI
            logger.info(f"No parent folder specified, creating album under root node: {parent_node_uri}")

        # URL for creating children under the parent node
        creation_url = f"https://api.smugmug.com{parent_node_uri}!children"
        logger.info(f"Creating album '{album_name}' using POST to: {creation_url}")

        try:
            # Prepare the album creation request (form-urlencoded for node children)
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            # Album settings - ensure URL name is valid
            # Generate a basic URL-safe name from the title
            url_name_base = ''.join(c for c in album_name if c.isalnum() or c in (' ', '-')).strip().title().replace(' ', '')
            # Limit length and ensure it's not empty
            url_name = url_name_base[:50] if url_name_base else f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}"
            if not url_name: # Failsafe if above logic somehow results in empty
                 url_name = f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}"
                 logger.warning(f"Generated fallback UrlName '{url_name}' for album '{album_name}'")
            # Ensure starts with a capital letter if possible (required by API)
            if url_name and not url_name[0].isupper():
                url_name = url_name[0].upper() + url_name[1:]


            # Define desired attributes using correct types based on latest findings
            album_data = {
                'Name': album_name,
                'UrlName': url_name,
                'Type': 'Album',              # Required for creation
                'Privacy': 'Private',         # String - Desired default
                'LargestSize': 'Original',    # String - Desired default
                'Protected': False,           # Boolean - Desired default (Protection OFF)
                'SmugSearchable': 'No',     # String - Desired default
                'WorldSearchable': False,     # Boolean - Desired default
                'AllowDownloads': True        # Boolean - Desired default
                # Add other defaults if needed, e.g., Comments=True, Clean=False, etc.
                #'Comments': True,
                #'Clean': False,
            }

            response = self.auth_session.post(creation_url, headers=headers, data=album_data)
            response.raise_for_status()
            data = response.json()

            # Extract the album key and URI from the newly created Node response
            new_node = data.get('Response', {}).get('Node')
            if not new_node or new_node.get('Type') != 'Album':
                logger.error(f"Album creation POST successful, but response did not contain expected Album Node.")
                logger.error(f"Response data: {json.dumps(data, indent=2)}")
                return None, None

            # Get the Album URI specifically from the Node's Uris structure
            album_uri_info = new_node.get('Uris', {}).get('Album')
            if not album_uri_info or not isinstance(album_uri_info, dict):
                 logger.error(f"Album Node created, but missing 'Album' URI structure.")
                 logger.debug(f"Node data: {json.dumps(new_node, indent=2)}")
                 return None, None

            album_uri = album_uri_info.get('Uri')
            # Parse the AlbumKey from the end of the album_uri
            album_key = album_uri.split('/')[-1] if album_uri else None

            if not album_key or not album_uri:
                 logger.error(f"Album Node created, but failed to extract AlbumKey ('{album_key}') or Album URI ('{album_uri}').")
                 logger.debug(f"Node data: {json.dumps(new_node, indent=2)}")
                 return None, None

            logger.info(f"Successfully created album '{album_name}'. Album Key: {album_key}, Album URI: {album_uri}")

            # Set instance attributes
            self.album_key = album_key
            self.album_api_uri = album_uri
            self.album_name = album_name # Use the name we created it with

            # Save album_key and album_api_uri to config file
            if self.config:
                self.config['album_key'] = album_key
                self.config['album_api_uri'] = album_uri
                # Clear album_name from config if key/uri are now set (optional, but good practice)
                # Check if album_name exists before trying to modify it
                if 'album_name' in self.config:
                    self.config['album_name'] = DEFAULT_SMUGMUG_CONFIG['album_name']
                if not self.save_config():
                    logger.warning("Failed to save updated album key/uri to config file after creation.")
            else:
                 logger.warning("Config object is None. Cannot save album details to config.")

            # Validate and correct album attributes after creation (handles potential discrepancies)
            self.validate_and_correct_album_attributes(album_uri)

            return album_key, album_uri

        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating album '{album_name}' on SmugMug: {e}", exc_info=True)
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"HTTP Status Code: {e.response.status_code}")
                try:
                    logger.error(f"SmugMug API Response Text: {e.response.text}")
                except Exception:
                    logger.error("Could not decode SmugMug API response text.")
            return None, None
        except Exception as e:
            logger.error(f"An unexpected error occurred while creating album '{album_name}' on SmugMug: {e}", exc_info=True)
            return None, None
    # --- End CORRECTED create_album ---

    def validate_and_correct_album_attributes(self, album_uri):
        """Validates and corrects the attributes of an album via PATCH request."""
        # --- Function completely revised based on live API data analysis ---
        if not self.auth_session or not album_uri:
            logger.error("Cannot validate/correct album: Not authenticated or no album URI.")
            return

        logger.info(f"Validating and correcting attributes for album URI: {album_uri}")

        # Define desired state based on live API data analysis
        desired_attributes = {
            'LargestSize': 'Original',  # String
            'Protected': False,         # Boolean (Represents Right-Click Protection Off)
            'SmugSearchable': 'No',     # String
            'WorldSearchable': False,   # Boolean
            'AllowDownloads': True,     # Boolean
            'Privacy': 'Private'        # String
        }

        url = f"https://api.smugmug.com{album_uri}"
        headers = {'Accept': 'application/json'}
        patch_payload = {}

        try:
            # Get current album data
            response = self.auth_session.get(url, headers=headers)
            response.raise_for_status()
            current_data = response.json().get('Response', {}).get('Album', {})
            if not current_data:
                logger.error("Could not fetch current album data for validation.")
                return

            # Compare current vs desired with refined logic
            for attr, desired in desired_attributes.items():
                current = current_data.get(attr)
                needs_patch = False # Flag to indicate if this attribute needs patching

                if current is None and desired is not None:
                     # If current value is missing but we desire a specific value
                     needs_patch = True
                     logger.warning(f"Attribute '{attr}' missing in current data, Desired='{desired}'. Adding to patch.")
                elif current is not None:
                    current_str_lower = str(current).lower()

                    # --- Handle Boolean desired values ---
                    # Covers Protected, AllowDownloads, WorldSearchable
                    if isinstance(desired, bool):
                        # Equivalent "false" strings: 'false', '0'
                        # Equivalent "true" strings: 'true', '1'
                        desired_equiv_strs = (str(desired).lower(), str(int(desired)))
                        if current_str_lower not in desired_equiv_strs:
                            needs_patch = True

                    # --- Handle String 'No' desired value ---
                    # Covers SmugSearchable
                    elif isinstance(desired, str) and desired.lower() == 'no':
                        # Equivalent "false" strings: 'false', '0', 'no'
                        valid_false_strs = ('false', '0', 'no')
                        if current_str_lower not in valid_false_strs:
                            needs_patch = True # Patch if current isn't false/0/no

                    # --- Handle other String desired values ---
                    # Covers LargestSize, Privacy
                    elif isinstance(desired, str) and current != desired:
                         # Simple string comparison for other attributes
                         needs_patch = True

                    # --- Log if patch is needed ---
                    if needs_patch:
                        logger.warning(f"Discrepancy in '{attr}': Current='{current}' (Type: {type(current)}), Desired='{desired}' (Type: {type(desired)}). Adding to patch.")
                        patch_payload[attr] = desired # Add the desired value (with correct type)

            # Send PATCH request if needed
            if patch_payload:
                logger.info(f"Sending PATCH to correct attributes: {patch_payload}")
                patch_headers = headers.copy()
                patch_headers['Content-Type'] = 'application/json'
                patch_response = self.auth_session.patch(url, headers=patch_headers, json=patch_payload)
                patch_response.raise_for_status()
                try:
                    patch_result = patch_response.json()
                    logger.debug(f"SmugMug PATCH response: {json.dumps(patch_result, indent=2)}")
                except json.JSONDecodeError:
                     logger.debug(f"SmugMug PATCH response status: {patch_response.status_code} (No JSON body or decode error)")

                logger.info("Successfully corrected album attributes via PATCH.")
            else:
                logger.info("Album attributes are already correct.")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error during album attribute validation/correction for {album_uri}: {e}", exc_info=True)
            if hasattr(e, 'response') and e.response is not None:
                 logger.error(f"HTTP Status Code: {e.response.status_code}")
                 try: logger.error(f"SmugMug API Response Text: {e.response.text}")
                 except Exception: logger.error("Could not decode SmugMug API response text.")
        except Exception as e:
            logger.error(f"Unexpected error during album validation/correction: {e}", exc_info=True)
    # --- End validate_and_correct_album_attributes ---


    def get_or_create_folder(self, folder_name, parent_node_uri=None):
        """
        Gets an existing folder by name under parent_node_uri (or root) or creates it.
        Returns the folder's Node URI if successful, None otherwise.
        """
        if not folder_name:
            logger.debug("No folder name specified, skipping folder get/create.")
            return None # No folder needed

        logger.info(f"Attempting to find or create folder: '{folder_name}' under node: {parent_node_uri or 'root'}")
        folder_node_uri = self.select_folder_by_name(folder_name, parent_node_uri=parent_node_uri)

        if folder_node_uri:
            logger.info(f"Found existing folder '{folder_name}' with Node URI: {folder_node_uri}")
            return folder_node_uri
        else:
            logger.info(f"Folder '{folder_name}' not found. Creating new folder...")
            return self.create_folder(folder_name, parent_node_uri=parent_node_uri)

    def create_folder(self, folder_name, parent_node_uri=None):
        """
        Creates a new folder under the specified parent node URI (or root node).
        Returns the new folder's Node URI if successful, None otherwise.
        """
        if not self.auth_session:
            logger.error("Not authenticated with SmugMug. Cannot create folder.")
            return None
        if not folder_name:
            logger.error("Folder name not provided. Cannot create folder.")
            return None

        if not parent_node_uri:
             # Default to creating under the user's root node
            _, user_data = self.get_user_endpoint()
            if not user_data: return None
            parent_node_uri = self._get_node_uri(user_data)
            if not parent_node_uri: return None
            logger.info(f"No parent specified, creating folder under root node: {parent_node_uri}")

        # URL for creating children under the parent node
        creation_url = f"https://api.smugmug.com{parent_node_uri}!children"
        logger.info(f"Creating folder '{folder_name}' using POST to: {creation_url}")

        try:
            # Prepare the folder creation request (form-urlencoded for node children)
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'
            }
             # Ensure URL name is valid
            url_name_base = ''.join(c for c in folder_name if c.isalnum() or c in (' ', '-')).strip().title().replace(' ', '')
            url_name = url_name_base[:50] if url_name_base else f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"
            if not url_name: # Failsafe
                 url_name = f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"
                 logger.warning(f"Generated fallback UrlName '{url_name}' for folder '{folder_name}'")
            if url_name and not url_name[0].isupper(): # Ensure starts with capital
                 url_name = url_name[0].upper() + url_name[1:]


            folder_data = {
                'Name': folder_name,
                'UrlName': url_name,
                'Type': 'Folder',
                'Privacy': 'Private' # Default new folders to private
            }

            response = self.auth_session.post(creation_url, headers=headers, data=folder_data)
            response.raise_for_status()
            data = response.json()

            # Extract the folder's Node URI from the newly created Node response
            new_node = data.get('Response', {}).get('Node')
            if not new_node or new_node.get('Type') != 'Folder':
                 logger.error(f"Folder creation POST successful, but response did not contain expected Folder Node.")
                 logger.error(f"Response data: {json.dumps(data, indent=2)}")
                 return None

            new_folder_node_uri = new_node.get('Uri') # The Node URI is the 'Uri' field itself
            if not new_folder_node_uri:
                 logger.error(f"Folder Node created, but missing Node URI.")
                 logger.debug(f"Node data: {json.dumps(new_node, indent=2)}")
                 return None

            logger.info(f"Successfully created folder '{folder_name}'. Node URI: {new_folder_node_uri}")
            return new_folder_node_uri

        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating folder '{folder_name}' on SmugMug: {e}", exc_info=True)
            if hasattr(e, 'response') and e.response is not None:
                 logger.error(f"HTTP Status Code: {e.response.status_code}")
                 try:
                    logger.error(f"SmugMug API Response Text: {e.response.text}")
                 except Exception:
                    logger.error("Could not decode SmugMug API response text.")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred while creating folder '{folder_name}' on SmugMug: {e}", exc_info=True)
            return None

    def get_or_create_album_in_path(self, album_name, folder_path_str=None):
         """
         Ensures an album exists at the specified path (creating folders as needed).
         Sets self.album_key and self.album_api_uri on success.
         Returns True if successful, False otherwise.
         folder_path_str: Path like "Folder1/SubFolder2" or None for root.
         """
         if not self.auth_session: return False
         if not album_name:
              logger.error("Album name required.")
              return False

         target_parent_node_uri = None # Start assuming root

         # Create/Find folders if path is specified
         if folder_path_str:
              folder_names = [name.strip() for name in folder_path_str.split('/') if name.strip()]
              current_parent_node_uri = None # Start at root for path traversal

              for i, name in enumerate(folder_names):
                   logger.info(f"Ensuring folder '{name}' exists under node: {current_parent_node_uri or 'root'}")
                   found_node_uri = self.get_or_create_folder(name, parent_node_uri=current_parent_node_uri)
                   if not found_node_uri:
                        logger.error(f"Failed to find or create folder '{name}' in path '{folder_path_str}'.")
                        return False
                   current_parent_node_uri = found_node_uri # This becomes the parent for the next iteration or the album

              target_parent_node_uri = current_parent_node_uri # The last folder found/created is the target parent

         # Now find or create the album under the target parent node
         logger.info(f"Ensuring album '{album_name}' exists under node: {target_parent_node_uri or 'root'}")
         if self.select_album_by_name(album_name, parent_node_uri=target_parent_node_uri):
              logger.info(f"Album '{album_name}' found.")
              # Make sure attributes are correct even if album already existed
              self.validate_and_correct_album_attributes(self.album_api_uri)
              return True # Album found
         else:
              logger.info(f"Album '{album_name}' not found, creating...")
              # create_album now includes validation
              key, uri = self.create_album(album_name, parent_node_uri=target_parent_node_uri)
              if key and uri:
                   logger.info(f"Album '{album_name}' created successfully.")
                   return True # Album created
              else:
                   logger.error(f"Failed to create album '{album_name}'.")
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
