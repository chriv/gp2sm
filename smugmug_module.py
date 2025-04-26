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
import sys

# Third-party imports
import requests
from requests_oauthlib import OAuth1Session

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
                logging.info(f"Successfully loaded SmugMug config from {self.config_file}")
                return True
        except FileNotFoundError:
            # Let this exception propagate up to main.py
            raise
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding JSON from SmugMug config file: {self.config_file} - {e}")
            self.config = None
            return False
        except Exception as e:
            logging.error(f"Unexpected error loading SmugMug config {self.config_file}: {e}")
            self.config = None
            return False

    def generate_default_config(self):
        """Generates a default SmugMug config file with placeholders."""
        logging.warning(f"SmugMug config file '{self.config_file}' not found. Generating default.")
        try:
            with open(self.config_file, 'w') as f:
                # Use sort_keys=False to preserve the order defined in DEFAULT_SMUGMUG_CONFIG
                json.dump(DEFAULT_SMUGMUG_CONFIG, f, indent=2, sort_keys=False)
            logging.info(f"Default SmugMug config file created at '{self.config_file}'.")
            logging.info("Please edit this file to add your API Key and Secret, and configure your target album.")
            print(f"\nDefault SmugMug config file created at '{self.config_file}'.")
            print("--> Please edit this file to add your SmugMug API Key and Secret.")
            print("--> You also need to specify either 'album_name' OR 'album_key'/'album_api_uri'.")
            print("--> Then run the script again.")
            return True
        except IOError as e:
            logging.error(f"Error generating default SmugMug config file '{self.config_file}': {e}")
            print(f"Error: Could not write default SmugMug config file to '{self.config_file}'. Check permissions.")
            return False

    def save_config(self):
        """Saves the current SmugMug API configuration to its file."""
        if not self.config:
            logging.error("No SmugMug configuration loaded to save.")
            return False

        try:
            # Ensure comments and default values are present before saving
            temp_config = DEFAULT_SMUGMUG_CONFIG.copy() # Start with defaults
            temp_config.update(self.config) # Update with current values
            self.config = temp_config # Replace self.config with merged version

            with open(self.config_file, 'w') as f:
                 # Use sort_keys=False to preserve the order defined in DEFAULT_SMUGMUG_CONFIG
                json.dump(self.config, f, indent=2, sort_keys=False)
            logging.info(f"SmugMug configuration saved to {self.config_file}")
            return True
        except IOError as e:
            logging.error(f"Error saving SmugMug configuration to {self.config_file}: {e}")
            print(f"Error: Could not save updated SmugMug config to '{self.config_file}'. Check permissions.")
            return False
        except Exception as e:
             logging.error(f"Unexpected error saving SmugMug config: {e}")
             return False

    def obtain_oauth_tokens(self, api_key, api_secret):
        """Obtains SmugMug OAuth tokens using the OAuth 1.0a flow."""
        request_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getRequestToken'
        # Request full access and modify permissions
        authorize_url = 'https://secure.smugmug.com/services/oauth/1.0a/authorize?Access=Full&Permissions=Modify'
        access_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getAccessToken'

        # 'oob' (Out-Of-Band) is standard for desktop/CLI applications
        smugmug = OAuth1Session(api_key, client_secret=api_secret, callback_uri='oob')
        logging.info("Fetching request token from SmugMug...")
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
                logging.info("Fetching access token from SmugMug...")
                token_response = smugmug.fetch_access_token(access_token_url, verifier=verifier)
                if token_response:
                    oauth_token = token_response.get('oauth_token')
                    oauth_token_secret = token_response.get('oauth_token_secret')
                    if oauth_token and oauth_token_secret:
                         logging.info("Successfully obtained SmugMug access tokens.")
                         return oauth_token, oauth_token_secret
                    else:
                         logging.error("OAuth token response did not contain expected token/secret.")
                         return None, None
                else:
                    logging.error("Failed to fetch access token from SmugMug. Response was empty or invalid (maybe incorrect verifier code?).")
                    return None, None
            else:
                logging.error("Failed to fetch request token from SmugMug. Check API key/secret or network.")
                return None, None
        except ValueError as ve:
             logging.error(f"Error during SmugMug OAuth flow (potentially invalid verifier code?): {ve}")
             return None, None
        except Exception as e:
            logging.error(f"An error occurred during SmugMug OAuth flow: {e}", exc_info=True)
            return None, None

    def check_config_and_authenticate(self):
        """
        Checks for essential config keys, updates config if needed, performs OAuth,
        and verifies authentication. Returns True on success, False otherwise.
        """
        if not self.config:
            logging.error("SmugMug configuration is not loaded. Cannot authenticate.")
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
            logging.error(f"Missing or placeholder values in '{self.config_file}' for: {', '.join(missing_keys)}")
            logging.error("Please obtain these from SmugMug (https://api.smugmug.com/api/developer/apply) and update the config file.")
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
            logging.info("SmugMug OAuth tokens not found or are placeholders in config. Initiating authorization flow...")
            new_oauth_token, new_oauth_token_secret = self.obtain_oauth_tokens(api_key, api_secret)
            if new_oauth_token and new_oauth_token_secret:
                self.config['oauth_token'] = new_oauth_token
                self.config['oauth_token_secret'] = new_oauth_token_secret
                # Update local variables for immediate use
                oauth_token = new_oauth_token
                oauth_token_secret = new_oauth_token_secret
                # Save the newly obtained tokens to the config file
                if not self.save_config():
                     logging.error("Failed to save SmugMug config after obtaining OAuth tokens.")
                     print("\nWarning: Failed to save updated SmugMug tokens to config file. You may need to authorize again next time.")
                else:
                     logging.info("Successfully obtained and saved SmugMug OAuth tokens.")
                     config_updated = True # Mark as updated (though already saved)
            else:
                logging.error("Failed to obtain SmugMug OAuth tokens. Please check logs/API keys and try again.")
                print("\nError: Failed to obtain SmugMug authorization. Please ensure API key/secret are correct and try again.")
                return False
        else:
             logging.debug("Found existing SmugMug OAuth tokens in config.")

        # --- Create Auth Session ---
        try:
             self.auth_session = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token,
                                               resource_owner_secret=oauth_token_secret)
             logging.info("SmugMug OAuth session created.")
             # Verify authentication with a lightweight API call
             _, user_data = self.get_user_endpoint() # This implicitly checks authentication
             if not user_data:
                  raise Exception("Failed to verify SmugMug authentication via user endpoint (returned None).")
             logging.info(f"SmugMug authentication successful for user: {self.username}")

        except Exception as auth_err:
             logging.error(f"SmugMug authentication failed: {auth_err}", exc_info=True)
             print("\nError: SmugMug authentication failed. Tokens might be invalid or expired.")
             print(f"--> Attempting to clear potentially invalid tokens from '{self.config_file}'. Please run again to re-authorize.")
             # Clear potentially invalid tokens
             self.config['oauth_token'] = placeholders['oauth_token']
             self.config['oauth_token_secret'] = placeholders['oauth_token_secret']
             self.save_config() # Save cleared tokens
             self.auth_session = None # Ensure session is cleared
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
            logging.info(f"Derived album_api_uri from valid album_key: {album_api_uri}")
            # Update config if derived - mark for saving
            if self.config.get('album_api_uri') != album_api_uri:
                 self.config['album_api_uri'] = album_api_uri
                 config_updated = True

        has_valid_album_name = bool(album_name)
        has_valid_album_key_uri = bool(album_key) and bool(album_api_uri)

        # Check if at least one valid album specification method exists
        if not has_valid_album_name and not has_valid_album_key_uri:
            logging.error(f"Missing album configuration in '{self.config_file}'.")
            logging.error("Please specify either a valid 'album_name' OR both 'album_key' and 'album_api_uri'.")
            print(f"\nError: Missing SmugMug album configuration in '{self.config_file}'.")
            print("--> Please edit the file and provide either 'album_name' OR 'album_key' & 'album_api_uri'.")

            # Add missing placeholders if needed, mark for saving
            needs_save = False
            if "album_name" not in self.config:
                self.config["album_name"] = placeholders["album_name"]
                self.config["_comment_album_name"] = DEFAULT_SMUGMUG_CONFIG["_comment_album_name"]
                needs_save = True
            if "album_key" not in self.config:
                self.config["album_key"] = placeholders["album_key"]
                self.config["album_api_uri"] = placeholders["album_api_uri"]
                self.config["_comment_album_key"] = DEFAULT_SMUGMUG_CONFIG["_comment_album_key"]
                needs_save = True
            # Add other missing optional keys/comments if desired (folder_name, process_heic)
            if "folder_name" not in self.config:
                 self.config["folder_name"] = placeholders["folder_name"]
                 self.config["_comment_folder"] = DEFAULT_SMUGMUG_CONFIG["_comment_folder"]
                 needs_save = True
            if "process_heic" not in self.config:
                 self.config["process_heic"] = DEFAULT_SMUGMUG_CONFIG["process_heic"]
                 self.config["_comment_heic"] = DEFAULT_SMUGMUG_CONFIG["_comment_heic"]
                 needs_save = True

            if needs_save:
                self.save_config()
            return False

        # --- Set internal attributes based on valid config (Priority: album_name > album_key/uri) ---
        if has_valid_album_name:
            self.album_name = album_name
            self.album_key = None
            self.album_api_uri = None
            logging.info(f"Using SmugMug album name from config: '{self.album_name}'")
        elif has_valid_album_key_uri:
            # Use the potentially derived URI if name wasn't valid
            self.album_name = None
            self.album_key = album_key
            self.album_api_uri = album_api_uri
            logging.info(f"Using SmugMug album key from config: '{self.album_key}' (URI: {self.album_api_uri})")
        else:
             # Should be caught above, but as a failsafe:
             logging.critical("Critical internal error: No valid SmugMug album config found.")
             return False

        self.folder_name = folder_name # Set folder name attribute
        if self.folder_name:
             logging.info(f"Using SmugMug folder name from config: '{self.folder_name}'")

        # --- Check process_heic (add if missing) ---
        if "process_heic" not in self.config:
            logging.info("Adding missing 'process_heic' setting (default: false) to config.")
            self.config["process_heic"] = DEFAULT_SMUGMUG_CONFIG["process_heic"]
            self.config["_comment_heic"] = DEFAULT_SMUGMUG_CONFIG["_comment_heic"]
            config_updated = True # Mark for saving if not already marked

        # Final save if any updates were made (like deriving URI or adding process_heic)
        if config_updated:
              if not self.save_config():
                   logging.warning("Failed to save SmugMug config after adding missing/derived keys.")

        return True # All checks passed, authenticated


    def check_media_exists(self, album_key, filename, mime_type, file_hash=None):
        """
        Checks if a media item exists in the specified SmugMug album.
        - For images (mime_type starting with 'image/'), checks by MD5 hash.
        - For videos (mime_type starting with 'video/'), checks by filename.
        Returns True if found, False otherwise.
        """
        if not self.auth_session:
            logging.warning("SmugMug authentication not set up. Cannot check for media existence.")
            return False

        # Use the instance's confirmed album_key if the passed one is None
        if not album_key:
            album_key = self.album_key
            if not album_key:
                logging.warning("No SmugMug Album Key available. Cannot check for media existence.")
                return False

        is_video = mime_type.startswith('video/')
        # MD5 is required for images per SmugMug API for existence checks
        if not is_video and not file_hash:
            logging.warning(f"Image file '{filename}' requires an MD5 hash for existence check, but none was provided.")
            return False  # Cannot check image without hash

        # Construct the API URL to list images in the album
        # Need to fetch the album details first to get the AlbumImages URI
        album_details_url = f"https://api.smugmug.com/api/v2/album/{album_key}"
        headers = {'Accept': 'application/json'}

        try:
            # First, get album details to find the Images URI
            logging.debug(f"Fetching album details from: {album_details_url}")
            album_response = self.auth_session.get(album_details_url, headers=headers)
            album_response.raise_for_status()  # Raise for HTTP errors
            album_data = album_response.json()

            # Navigate through the response to find the AlbumImages URI
            images_uri = album_data.get('Response', {}).get('Album', {}).get('Uris', {}).get('AlbumImages', {}).get('Uri')
            if not images_uri:
                 logging.error(f"Could not find AlbumImages URI for SmugMug album key {album_key}. Cannot check existence.")
                 logging.debug(f"Album details response: {json.dumps(album_data, indent=2)}")
                 return False

            # Construct base URL for listing images, add count for pagination efficiency
            images_list_url = f"https://api.smugmug.com{images_uri}?count=100"
            next_page_url = images_list_url # Start with the first page

            logging.debug(f"Starting media existence check in album {album_key} using URI: {images_uri}")

            # Now, iterate through the album images using the found URI
            page_count = 0
            while next_page_url:
                page_count += 1
                logging.debug(f"Checking SmugMug images page {page_count}: {next_page_url}")
                response = self.auth_session.get(next_page_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                items_in_response = data.get('Response', {}).get('AlbumImage', []) # Ensure it's a list
                if items_in_response:
                    for item in items_in_response:
                        # Ensure item is a dictionary before accessing keys
                        if not isinstance(item, dict):
                             logging.warning(f"Found non-dictionary item in AlbumImage list: {item}")
                             continue

                        item_filename = item.get('FileName')

                        # Check based on media type
                        if is_video:
                            # For videos, compare filenames (case-insensitive)
                            if item_filename and filename and filename.lower() == item_filename.lower():
                                logging.info(f"Video '{filename}' found on SmugMug by filename match.")
                                return True
                        else:
                            # For images, compare MD5 hashes (case-insensitive)
                            smugmug_md5 = item.get('ArchivedMD5') # This field stores the MD5 hash
                            if file_hash and smugmug_md5 and file_hash.lower() == smugmug_md5.lower():
                                logging.info(f"Image '{filename}' found on SmugMug with matching MD5 hash: {file_hash}")
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
                    if "?count=" not in next_page_url:
                        separator = "&" if "?" in next_page_url else "?"
                        next_page_url += f"{separator}count=100"
                else:
                    next_page_url = None # No more pages

            # If the loop finishes without finding the item, it doesn't exist
            logging.debug(f"Media '{filename}' not found in SmugMug album {album_key} after checking all pages.")
            return False

        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking for media existence on SmugMug (filename: {filename}): {e}", exc_info=True)
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            # Consider returning False cautiously - allows retry/upload attempt
            return False
        except Exception as e:
            logging.error(f"An unexpected error occurred during SmugMug existence check for '{filename}': {e}", exc_info=True)
            return False # Assume it doesn't exist on unexpected error

    def upload_media(self, album_api_uri, file_path, filename, mime_type):
        """
        Upload media file to specified Album API URI.
        Returns True if successful, False otherwise. Cleans up temp file on success/failure.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot upload media.")
            return False

        # Use instance's confirmed album_api_uri if passed one is None
        if not album_api_uri:
            album_api_uri = self.album_api_uri
            if not album_api_uri:
                logging.error("No SmugMug Album API URI available. Cannot upload media.")
                return False

        if not file_path or not os.path.exists(file_path):
            logging.error(f"Upload failed: File not found at {file_path}")
            return False

        upload_successful = False
        try:
            # Calculate MD5 hash for the uploaded file content as required by SmugMug
            file_md5 = self.calculate_file_hash(file_path, hash_algorithm='md5')
            if not file_md5:
                logging.error(f"Failed to calculate MD5 hash for {filename}. Cannot upload.")
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

            logging.info(f"Attempting upload for '{filename}' ({mime_type}, {len(media_data)} bytes) to SmugMug album URI: {album_api_uri}")
            logging.debug(f"Upload Headers: {headers}")

            # The upload URL is fixed: https://upload.smugmug.com/
            upload_url = 'https://upload.smugmug.com/'
            response = self.auth_session.post(upload_url, headers=headers, data=media_data)

            logging.debug(f"Upload response status code: {response.status_code}")
            logging.debug(f"Upload response text: {response.text[:500]}...") # Log first 500 chars

            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            upload_data = response.json()

            # Check the response structure for success indication
            # Successful upload should return stat='ok' and contain an 'Image' or 'Video' object
            if upload_data.get('stat') == 'ok' and ('Image' in upload_data or 'Video' in upload_data):
                media_info = upload_data.get('Image') or upload_data.get('Video') # Get whichever is present
                status_url = media_info.get('StatusUri') if media_info else 'N/A' # Use StatusUri if available
                final_url = media_info.get('WebUri') if media_info else 'N/A' # Use WebUri if available

                logging.info(f"Successfully initiated upload for '{filename}'. SmugMug Status URI: {status_url}, Final URL (approx): {final_url}")
                upload_successful = True # Mark as successful for finally block logic
                return True
            else:
                logging.error(f"Failed to upload '{filename}' to SmugMug. Unexpected response format or status.")
                logging.error(f"SmugMug Upload Response: {json.dumps(upload_data, indent=2)}")
                return False

        except requests.exceptions.HTTPError as http_err:
             logging.error(f"HTTP Error uploading {filename} to SmugMug: {http_err}", exc_info=True)
             if http_err.response is not None:
                  logging.error(f"HTTP Status Code: {http_err.response.status_code}")
                  logging.error(f"SmugMug API Response Text: {http_err.response.text}")
             return False
        except requests.exceptions.RequestException as req_err:
            logging.error(f"Request Error uploading {filename} to SmugMug: {req_err}", exc_info=True)
            return False
        except Exception as e:
            logging.error(f"An unexpected error occurred during upload of '{filename}': {e}", exc_info=True)
            return False
        finally:
            # Clean up the temporary file ONLY if it exists AND upload was attempted
            # Avoid removing if it never existed or hash calc failed earlier
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logging.debug(f"Removed temporary upload file: {file_path}")
                except OSError as e:
                    logging.warning(f"Could not remove temporary file {file_path} after upload attempt: {e}")

    def get_user_endpoint(self):
        """
        Gets the authenticated user's details from the SmugMug API.
        Returns the username and user data dict if successful, None, None otherwise.
        Sets self.username on success.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot get user endpoint.")
            return None, None

        try:
            # Get the user data from the SmugMug API v2 entry point !authuser
            user_url = "https://api.smugmug.com/api/v2!authuser"
            headers = {'Accept': 'application/json'}
            logging.debug(f"Getting user endpoint from: {user_url}")
            response = self.auth_session.get(user_url, headers=headers)
            response.raise_for_status() # Check for HTTP errors
            user_data = response.json()

            # Extract the username (NickName) from the user data
            user_info = user_data.get('Response', {}).get('User', {})
            username = user_info.get('NickName')

            if not username:
                logging.error("Could not find username (NickName) in SmugMug API response.")
                logging.debug(f"User endpoint response: {json.dumps(user_data, indent=2)}")
                return None, None

            logging.debug(f"Found SmugMug username: {username}")
            self.username = username # Store the username

            return username, user_data # Return username and full response dict

        except requests.exceptions.RequestException as e:
            logging.error(f"Error getting user endpoint from SmugMug: {e}", exc_info=True)
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return None, None
        except Exception as e:
            logging.error(f"An unexpected error occurred while getting user endpoint from SmugMug: {e}", exc_info=True)
            return None, None

    def _get_node_uri(self, user_data):
         """Helper to extract the root Node URI from user data."""
         if not user_data: return None
         node_uri = user_data.get('Response', {}).get('User', {}).get('Uris', {}).get('Node', {}).get('Uri')
         if not node_uri:
              logging.error("Could not find root Node URI in user data.")
              logging.debug(f"User data for Node URI check: {json.dumps(user_data, indent=2)}")
         return node_uri

    def list_albums(self, node_uri=None):
        """
        Lists all albums under a specific node URI or the user's root node.
        Returns a list of album dictionaries if successful, empty list otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot list albums.")
            return []

        if not node_uri:
            # If no node_uri provided, get the user's root node
            _, user_data = self.get_user_endpoint()
            if not user_data: return []
            node_uri = self._get_node_uri(user_data)
            if not node_uri:
                logging.error("Cannot list albums: Failed to get root node URI.")
                return []
            logging.debug(f"Listing albums under root node: {node_uri}")
        else:
             logging.debug(f"Listing albums under provided node: {node_uri}")

        # Construct the URL to list albums under the specified node
        # The endpoint is typically nodeUri!albums
        albums_url = f"https://api.smugmug.com{node_uri}!albums?count=100" # Add count for pagination
        headers = {'Accept': 'application/json'}
        all_albums = []
        page_count = 0

        while albums_url:
            page_count += 1
            logging.debug(f"Fetching albums page {page_count} from: {albums_url}")
            try:
                response = self.auth_session.get(albums_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                albums_in_response = data.get('Response', {}).get('Album', []) # Ensure list
                if albums_in_response:
                    all_albums.extend(albums_in_response)
                    logging.debug(f"Found {len(albums_in_response)} albums on page {page_count}. Total found: {len(all_albums)}")

                # Pagination
                pages_info = data.get('Response', {}).get('Pages')
                if pages_info and 'NextPage' in pages_info and pages_info['NextPage']:
                    next_page_uri = pages_info['NextPage']
                    if not next_page_uri.startswith("http"):
                         albums_url = f"https://api.smugmug.com{next_page_uri}"
                    else:
                         albums_url = next_page_uri
                    # Add count parameter if not already there
                    if "?count=" not in albums_url:
                         separator = "&" if "?" in albums_url else "?"
                         albums_url += f"{separator}count=100"
                else:
                    albums_url = None # No more pages

            except requests.exceptions.RequestException as e:
                logging.error(f"Error listing albums from SmugMug node {node_uri}: {e}", exc_info=True)
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    logging.error(f"SmugMug API Response Text: {e.response.text}")
                return [] # Return empty on error
            except Exception as e:
                logging.error(f"An unexpected error occurred while listing albums from node {node_uri}: {e}", exc_info=True)
                return []

        logging.info(f"Found total of {len(all_albums)} albums under node {node_uri}.")
        return all_albums

    def list_folders(self, node_uri=None):
        """
        Lists folders under a specific node URI or the user's root node.
        Returns a list of folder dictionaries if successful, empty list otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot list folders.")
            return []

        if not node_uri:
            # Get the user's root node URI if not provided
            _, user_data = self.get_user_endpoint()
            if not user_data: return []
            node_uri = self._get_node_uri(user_data)
            if not node_uri:
                 logging.error("Cannot list folders: Failed to get root node URI.")
                 return []
            logging.debug(f"Listing folders under root node: {node_uri}")
        else:
            logging.debug(f"Listing folders under provided node: {node_uri}")

        # Get the children of the specified node
        children_url = f"https://api.smugmug.com{node_uri}!children?count=100"
        headers = {'Accept': 'application/json'}
        all_folders = []
        page_count = 0

        while children_url:
             page_count += 1
             logging.debug(f"Fetching node children page {page_count} from: {children_url}")
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
                           logging.debug(f"Found {len(folders_on_page)} folders on page {page_count}. Total found: {len(all_folders)}")

                  # Pagination
                  pages_info = data.get('Response', {}).get('Pages')
                  if pages_info and 'NextPage' in pages_info and pages_info['NextPage']:
                      next_page_uri = pages_info['NextPage']
                      if not next_page_uri.startswith("http"):
                          children_url = f"https://api.smugmug.com{next_page_uri}"
                      else:
                           children_url = next_page_uri
                      # Add count parameter if not already there
                      if "?count=" not in children_url:
                           separator = "&" if "?" in children_url else "?"
                           children_url += f"{separator}count=100"
                  else:
                       children_url = None # No more pages

             except requests.exceptions.RequestException as e:
                  logging.error(f"Error listing children from SmugMug node {node_uri}: {e}", exc_info=True)
                  if hasattr(e, 'response') and hasattr(e.response, 'text'):
                      logging.error(f"SmugMug API Response Text: {e.response.text}")
                  return [] # Return empty on error
             except Exception as e:
                  logging.error(f"An unexpected error occurred while listing children from node {node_uri}: {e}", exc_info=True)
                  return []

        logging.info(f"Found total of {len(all_folders)} folders under node {node_uri}.")
        return all_folders

    def select_album_by_name(self, album_name, parent_node_uri=None):
        """
        Selects an album by name under a specific parent node URI (or root node).
        Returns True if found and sets instance attributes, False otherwise.
        """
        logging.debug(f"Attempting to select album '{album_name}' under node URI: {parent_node_uri or 'root'}")
        albums = self.list_albums(node_uri=parent_node_uri) # List albums under the specific node
        if not albums:
            # Already logged in list_albums if error occurred or no albums found
            logging.debug(f"No albums found under node {parent_node_uri or 'root'} to select from.")
            return False

        # Find the album with the matching name (case-insensitive)
        for album in albums:
             # Ensure album is a dictionary
             if not isinstance(album, dict):
                  logging.warning(f"Found non-dictionary item in albums list: {album}")
                  continue

             current_album_name = album.get('Name')
             if current_album_name and album_name and current_album_name.lower() == album_name.lower():
                album_key = album.get('AlbumKey')
                album_uri = album.get('Uri') # This is the Album's own API URI

                if not album_key or not album_uri:
                    logging.error(f"Album '{album_name}' found, but missing AlbumKey or Uri in data: {json.dumps(album, indent=2)}")
                    return False

                # Set instance variables on successful selection
                self.album_key = album_key
                self.album_api_uri = album_uri
                self.album_name = current_album_name # Store the exact name found
                logging.info(f"Selected existing album '{current_album_name}' with key {self.album_key} and URI {self.album_api_uri}")
                return True

        logging.debug(f"Album '{album_name}' not found under node URI: {parent_node_uri or 'root'}")
        return False

    def select_folder_by_name(self, folder_name, parent_node_uri=None):
        """
        Selects a folder by name under a specific parent node URI (or root node).
        Returns the folder's Node URI if found, None otherwise.
        """
        logging.debug(f"Attempting to select folder '{folder_name}' under node URI: {parent_node_uri or 'root'}")
        folders = self.list_folders(node_uri=parent_node_uri) # List folders under the specific node
        if not folders:
             # Already logged in list_folders if error occurred or no folders found
            logging.debug(f"No folders found under node {parent_node_uri or 'root'} to select from.")
            return None

        # Find the folder with the matching name (case-insensitive)
        for folder in folders:
             # Ensure folder is a dictionary
             if not isinstance(folder, dict):
                  logging.warning(f"Found non-dictionary item in folders list: {folder}")
                  continue

             current_folder_name = folder.get('Name')
             if current_folder_name and folder_name and current_folder_name.lower() == folder_name.lower():
                # We need the *Node* URI for the folder to create things under it
                folder_node_uri = folder.get('Uri') # The 'Uri' key on a folder node *is* its node URI

                if not folder_node_uri:
                    logging.error(f"Folder '{folder_name}' found, but missing Node Uri in data: {json.dumps(folder, indent=2)}")
                    return None

                logging.info(f"Selected existing folder '{current_folder_name}' with Node URI {folder_node_uri}")
                return folder_node_uri # Return the Node URI

        logging.debug(f"Folder '{folder_name}' not found under node URI: {parent_node_uri or 'root'}")
        return None

    def create_album(self, album_name, parent_node_uri=None):
        """
        Creates a new album under the specified parent node URI (or root node).
        Returns the album key and album URI if successful, None, None otherwise.
        Sets instance attributes and saves config on success. Includes attribute validation.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot create album.")
            return None, None
        if not album_name:
            logging.error("Album name not provided. Cannot create album.")
            return None, None

        if not parent_node_uri:
            # Default to creating under the user's root node
            _, user_data = self.get_user_endpoint()
            if not user_data: return None, None # Failed to get user data
            parent_node_uri = self._get_node_uri(user_data)
            if not parent_node_uri: return None, None # Failed to get root node URI
            logging.info(f"No parent folder specified, creating album under root node: {parent_node_uri}")

        # URL for creating children under the parent node
        creation_url = f"https://api.smugmug.com{parent_node_uri}!children"
        logging.info(f"Creating album '{album_name}' using POST to: {creation_url}")

        try:
            # Prepare the album creation request (form-urlencoded for node children)
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            # Album settings - ensure URL name is valid
            url_name = ''.join(filter(str.isalnum, album_name.title().replace(' ', ''))) # Basic URL name generation
            if not url_name: # Handle case where name has no alphanumeric chars
                 url_name = f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}" # Generate a fallback
                 logging.warning(f"Generated fallback UrlName '{url_name}' for album '{album_name}'")

            album_data = {
                'Name': album_name,
                'UrlName': url_name,
                'Type': 'Album',
                'Privacy': 'Private',
                'LargestSize': 'Original',
                'Protected': False, # Matches 'Off' for Right-Click Message
                'SmugSearchable': 'No',
                'WorldSearchable': 'No',
                'AllowDownloads': True
            }

            response = self.auth_session.post(creation_url, headers=headers, data=album_data)
            response.raise_for_status()
            data = response.json()

            # Extract the album key and URI from the newly created Node response
            new_node = data.get('Response', {}).get('Node')
            if not new_node or new_node.get('Type') != 'Album':
                logging.error(f"Album creation POST successful, but response did not contain expected Album Node.")
                logging.error(f"Response data: {json.dumps(data, indent=2)}")
                return None, None

            album_key = new_node.get('AlbumKey')
            album_uri = new_node.get('Uris', {}).get('Album', {}).get('Uri') # Get the Album URI specifically

            if not album_key or not album_uri:
                 logging.error(f"Album Node created, but missing AlbumKey ('{album_key}') or Album URI ('{album_uri}').")
                 logging.error(f"Node data: {json.dumps(new_node, indent=2)}")
                 return None, None

            logging.info(f"Successfully created album '{album_name}'. Album Key: {album_key}, Album URI: {album_uri}")

            # Set instance attributes
            self.album_key = album_key
            self.album_api_uri = album_uri
            self.album_name = album_name # Use the name we created it with

            # Save album_key and album_api_uri to config file
            if self.config:
                self.config['album_key'] = album_key
                self.config['album_api_uri'] = album_uri
                # Clear album_name from config if key/uri are now set
                # *** USE CORRECT REFERENCE HERE ***
                self.config['album_name'] = DEFAULT_SMUGMUG_CONFIG['album_name']
                if not self.save_config():
                    logging.warning("Failed to save updated album key/uri to config file after creation.")
            else:
                 logging.warning("Config object is None. Cannot save album details to config.")

            # Validate and correct album attributes after creation
            self.validate_and_correct_album_attributes(album_uri)

            return album_key, album_uri

        except requests.exceptions.RequestException as e:
            logging.error(f"Error creating album '{album_name}' on SmugMug: {e}", exc_info=True)
            if hasattr(e, 'response') and e.response is not None:
                logging.error(f"HTTP Status Code: {e.response.status_code}")
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return None, None
        except Exception as e:
            logging.error(f"An unexpected error occurred while creating album '{album_name}' on SmugMug: {e}", exc_info=True)
            return None, None

    def validate_and_correct_album_attributes(self, album_uri):
        """Validates and corrects the attributes of an album via PATCH request."""
        if not self.auth_session or not album_uri:
            logging.error("Cannot validate/correct album: Not authenticated or no album URI.")
            return

        logging.info(f"Validating and correcting attributes for album URI: {album_uri}")
        desired_attributes = {
            'LargestSize': 'Original',
            'Protected': False,
            'SmugSearchable': 'No',
            'WorldSearchable': 'No',
            'AllowDownloads': True,
            'Privacy': 'Private'
        }
        url = f"https://api.smugmug.com{album_uri}"
        headers = {'Accept': 'application/json'}
        patch_payload = {}

        try:
            response = self.auth_session.get(url, headers=headers)
            response.raise_for_status()
            current_data = response.json().get('Response', {}).get('Album', {})
            if not current_data:
                logging.error("Could not fetch current album data for validation.")
                return

            for attr, desired in desired_attributes.items():
                current = current_data.get(attr)
                # Handle boolean comparison (SmugMug might return ints 0/1 or bools)
                if isinstance(desired, bool):
                     if str(current).lower() not in ('true' if desired else 'false', str(int(desired))):
                          logging.warning(f"Discrepancy in '{attr}': Current='{current}', Desired='{desired}'.")
                          patch_payload[attr] = desired
                elif current != desired:
                    logging.warning(f"Discrepancy in '{attr}': Current='{current}', Desired='{desired}'.")
                    patch_payload[attr] = desired

            if patch_payload:
                logging.info(f"Sending PATCH to correct attributes: {patch_payload}")
                patch_headers = headers.copy()
                patch_headers['Content-Type'] = 'application/json'
                patch_response = self.auth_session.patch(url, headers=patch_headers, json=patch_payload)
                patch_response.raise_for_status()
                logging.info("Successfully corrected album attributes via PATCH.")
            else:
                logging.info("Album attributes are already correct.")

        except requests.exceptions.RequestException as e:
            logging.error(f"Error during album attribute validation/correction for {album_uri}: {e}", exc_info=True)
            if hasattr(e, 'response') and e.response is not None:
                 logging.error(f"HTTP Status Code: {e.response.status_code}")
                 logging.error(f"SmugMug API Response Text: {e.response.text}")
        except Exception as e:
            logging.error(f"Unexpected error during album validation/correction: {e}", exc_info=True)

    def get_or_create_folder(self, folder_name, parent_node_uri=None):
        """
        Gets an existing folder by name under parent_node_uri (or root) or creates it.
        Returns the folder's Node URI if successful, None otherwise.
        """
        if not folder_name:
            logging.debug("No folder name specified, skipping folder get/create.")
            return None # No folder needed

        logging.info(f"Attempting to find or create folder: '{folder_name}' under node: {parent_node_uri or 'root'}")
        folder_node_uri = self.select_folder_by_name(folder_name, parent_node_uri=parent_node_uri)

        if folder_node_uri:
            logging.info(f"Found existing folder '{folder_name}' with Node URI: {folder_node_uri}")
            return folder_node_uri
        else:
            logging.info(f"Folder '{folder_name}' not found. Creating new folder...")
            return self.create_folder(folder_name, parent_node_uri=parent_node_uri)

    def create_folder(self, folder_name, parent_node_uri=None):
        """
        Creates a new folder under the specified parent node URI (or root node).
        Returns the new folder's Node URI if successful, None otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot create folder.")
            return None
        if not folder_name:
            logging.error("Folder name not provided. Cannot create folder.")
            return None

        if not parent_node_uri:
             # Default to creating under the user's root node
            _, user_data = self.get_user_endpoint()
            if not user_data: return None
            parent_node_uri = self._get_node_uri(user_data)
            if not parent_node_uri: return None
            logging.info(f"No parent specified, creating folder under root node: {parent_node_uri}")

        # URL for creating children under the parent node
        creation_url = f"https://api.smugmug.com{parent_node_uri}!children"
        logging.info(f"Creating folder '{folder_name}' using POST to: {creation_url}")

        try:
            # Prepare the folder creation request (form-urlencoded for node children)
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'
            }
             # Ensure URL name is valid
            url_name = ''.join(filter(str.isalnum, folder_name.title().replace(' ', '')))
            if not url_name:
                 url_name = f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"
                 logging.warning(f"Generated fallback UrlName '{url_name}' for folder '{folder_name}'")

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
                 logging.error(f"Folder creation POST successful, but response did not contain expected Folder Node.")
                 logging.error(f"Response data: {json.dumps(data, indent=2)}")
                 return None

            new_folder_node_uri = new_node.get('Uri') # The Node URI is the 'Uri' field itself
            if not new_folder_node_uri:
                 logging.error(f"Folder Node created, but missing Node URI.")
                 logging.error(f"Node data: {json.dumps(new_node, indent=2)}")
                 return None

            logging.info(f"Successfully created folder '{folder_name}'. Node URI: {new_folder_node_uri}")
            return new_folder_node_uri

        except requests.exceptions.RequestException as e:
            logging.error(f"Error creating folder '{folder_name}' on SmugMug: {e}", exc_info=True)
            if hasattr(e, 'response') and e.response is not None:
                 logging.error(f"HTTP Status Code: {e.response.status_code}")
                 logging.error(f"SmugMug API Response Text: {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred while creating folder '{folder_name}' on SmugMug: {e}", exc_info=True)
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
              logging.error("Album name required.")
              return False

         target_parent_node_uri = None # Start assuming root

         # Create/Find folders if path is specified
         if folder_path_str:
              folder_names = [name.strip() for name in folder_path_str.split('/') if name.strip()]
              current_parent_node_uri = None # Start at root for path traversal

              for i, name in enumerate(folder_names):
                   logging.info(f"Ensuring folder '{name}' exists under node: {current_parent_node_uri or 'root'}")
                   found_node_uri = self.get_or_create_folder(name, parent_node_uri=current_parent_node_uri)
                   if not found_node_uri:
                        logging.error(f"Failed to find or create folder '{name}' in path '{folder_path_str}'.")
                        return False
                   current_parent_node_uri = found_node_uri # This becomes the parent for the next iteration or the album

              target_parent_node_uri = current_parent_node_uri # The last folder found/created is the target parent

         # Now find or create the album under the target parent node
         logging.info(f"Ensuring album '{album_name}' exists under node: {target_parent_node_uri or 'root'}")
         if self.select_album_by_name(album_name, parent_node_uri=target_parent_node_uri):
              logging.info(f"Album '{album_name}' found.")
              # Make sure attributes are correct even if album already existed
              self.validate_and_correct_album_attributes(self.album_api_uri)
              return True # Album found
         else:
              logging.info(f"Album '{album_name}' not found, creating...")
              key, uri = self.create_album(album_name, parent_node_uri=target_parent_node_uri)
              # create_album now includes validation
              if key and uri:
                   logging.info(f"Album '{album_name}' created successfully.")
                   return True # Album created
              else:
                   logging.error(f"Failed to create album '{album_name}'.")
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
            logging.error(f"Unsupported hash algorithm: {hash_algorithm}")
            return None

        try:
            with open(file_path, 'rb') as afile:
                # Read in chunks to handle large files efficiently
                while True:
                    # Read 64KB chunks
                    chunk = afile.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
            return hasher.hexdigest()
        except FileNotFoundError:
            logging.error(f"File not found for hashing: {file_path}")
            return None
        except Exception as e:
            logging.error(f"Error calculating {hash_algorithm.upper()} hash for {file_path}: {e}", exc_info=True)
            return None
