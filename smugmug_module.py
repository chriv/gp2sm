# SmugMug Module (v2.0 - Corrected Formatting & Logic)
# - Uses !children endpoint for folder/album checks for reliability.
# - Includes debug logging for raw API response text.
# - Adheres to one statement per line formatting.
# - Handles SmugMugAlbumFullError.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload

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
        self.config = None
        self.auth_session = None
        self.album_key = None
        self.album_api_uri = None
        self.album_name = None
        self.folder_name = None
        self.username = None
        self.user_uri = None

    def load_config(self):
        """Loads SmugMug API configuration."""
        try:
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
            logger.info(f"Successfully loaded SmugMug config from {self.config_file}")
            return True
        except FileNotFoundError:
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from {self.config_file}: {e}")
            self.config = None
            return False
        except Exception as e:
            logger.error(f"Error loading SmugMug config {self.config_file}: {e}")
            self.config = None
            return False

    def generate_default_config(self):
        """Generates a default SmugMug config file."""
        logger.warning(f"SmugMug config file '{self.config_file}' not found. Generating default.")
        try:
            with open(self.config_file, 'w') as f:
                json.dump(DEFAULT_SMUGMUG_CONFIG, f, indent=2, sort_keys=False)
            logger.info(f"Default SmugMug config file created: '{self.config_file}'. Please edit.")
            print(f"\nDefault SmugMug config created: '{self.config_file}'. Edit API keys/album details.")
            return True
        except IOError as e:
            logger.error(f"Error generating default config '{self.config_file}': {e}")
            return False

    def save_config(self):
        """Saves the current SmugMug API configuration."""
        if not self.config:
            logger.error("No SmugMug config loaded to save.")
            return False
        try:
            temp_config = DEFAULT_SMUGMUG_CONFIG.copy()
            temp_config.update(self.config)
            self.config = temp_config
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2, sort_keys=False)
            logger.info(f"SmugMug config saved to {self.config_file}")
            return True
        except IOError as e:
            logger.error(f"Error saving SmugMug config to {self.config_file}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error saving SmugMug config: {e}")
            return False

    def obtain_oauth_tokens(self, api_key, api_secret):
        """Obtains SmugMug OAuth tokens."""
        request_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getRequestToken'
        authorize_url = 'https://secure.smugmug.com/services/oauth/1.0a/authorize?Access=Full&Permissions=Modify'
        access_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getAccessToken'
        smugmug = OAuth1Session(api_key, client_secret=api_secret, callback_uri='oob')
        logger.info("Fetching SmugMug request token...")
        try:
            fetch_response = smugmug.fetch_request_token(request_token_url)
            if fetch_response:
                auth_url = smugmug.authorization_url(authorize_url)
                print(f"\n{'='*60}\nSmugMug Authorization Needed:\nOpen URL: {auth_url}\nEnter the 6-digit code below.\n{'='*60}")
                verifier = input("Verifier code: ").strip()
                logger.info("Fetching SmugMug access token...")
                token_response = smugmug.fetch_access_token(access_token_url, verifier=verifier)
                if token_response:
                    oauth_token = token_response.get('oauth_token')
                    oauth_token_secret = token_response.get('oauth_token_secret')
                    if oauth_token and oauth_token_secret:
                        logger.info("SmugMug access tokens obtained.")
                        return oauth_token, oauth_token_secret
                    else:
                        logger.error("OAuth response missing token/secret.")
                        return None, None
                else:
                    logger.error("Failed fetching access token (invalid verifier?).")
                    return None, None
            else:
                logger.error("Failed fetching request token (check API key/secret?).")
                return None, None
        except ValueError as ve:
            logger.error(f"OAuth error (invalid verifier?): {ve}")
            return None, None
        except Exception as e:
            logger.error(f"OAuth flow error: {e}", exc_info=True)
            return None, None

    def check_config_and_authenticate(self):
        """Checks config, performs OAuth, verifies auth. Returns True/False."""
        if not self.config:
            logger.error("SmugMug config not loaded.")
            return False

        placeholders = DEFAULT_SMUGMUG_CONFIG
        missing_keys = [k for k in ["api_key", "api_secret"] if not self.config.get(k) or self.config[k] == placeholders[k]]
        if missing_keys:
            logger.error(f"Missing credentials in '{self.config_file}': {missing_keys}")
            return False

        api_key = self.config['api_key']
        api_secret = self.config['api_secret']
        oauth_token = self.config.get('oauth_token')
        oauth_token_secret = self.config.get('oauth_token_secret')

        if not oauth_token or not oauth_token_secret or oauth_token == placeholders['oauth_token']:
            logger.info("SmugMug OAuth tokens needed. Starting authorization...")
            new_oauth_token, new_oauth_token_secret = self.obtain_oauth_tokens(api_key, api_secret)
            if new_oauth_token and new_oauth_token_secret:
                self.config['oauth_token'] = new_oauth_token
                self.config['oauth_token_secret'] = new_oauth_token_secret
                oauth_token = new_oauth_token
                oauth_token_secret = new_oauth_token_secret
                if not self.save_config():
                    logger.error("Failed saving new OAuth tokens.")
                else:
                    logger.info("Saved new SmugMug OAuth tokens.")
            else:
                logger.error("Failed obtaining OAuth tokens.")
                return False
        else:
            logger.debug("Found existing SmugMug OAuth tokens.")

        try:
             self.auth_session = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token, resource_owner_secret=oauth_token_secret)
             logger.info("SmugMug OAuth session created.")
             _, user_data = self.get_user_endpoint()
             if not user_data:
                 raise Exception("Failed verifying auth via user endpoint.")
             logger.info(f"SmugMug auth OK for user: {self.username}")
        except Exception as auth_err:
             logger.error(f"SmugMug auth failed: {auth_err}", exc_info=True)
             logger.debug(f"Raw auth error: {auth_err}")
             if "token_rejected" in str(auth_err) or "Invalid OAuth" in str(auth_err) or "Invalid Token" in str(auth_err):
                 print("\nError: SmugMug token rejected. Clearing tokens. Run again to re-authorize.")
                 self.config['oauth_token'] = placeholders['oauth_token']
                 self.config['oauth_token_secret'] = placeholders['oauth_token_secret']
                 self.save_config()
                 self.auth_session = None
                 return False
             else:
                 print(f"\nError: SmugMug auth failed: {auth_err}")
                 return False

        album_name = self.config.get('album_name') if self.config.get('album_name') != placeholders['album_name'] else None
        album_key = self.config.get('album_key') if self.config.get('album_key') != placeholders['album_key'] else None
        album_api_uri = self.config.get('album_api_uri') if self.config.get('album_api_uri') != placeholders['album_api_uri'] else None
        folder_name = self.config.get('folder_name') if self.config.get('folder_name') != placeholders['folder_name'] else None

        if album_key and not album_api_uri:
            album_api_uri = f'/api/v2/album/{album_key}'
            self.config['album_api_uri'] = album_api_uri
            self.save_config()

        if not album_name and not (album_key and album_api_uri):
            logger.error("Missing album config. Set 'album_name' OR 'album_key'/'album_api_uri'.")
            return False

        if album_name and album_key:
            logger.warning("Both 'album_name' and 'album_key' set. Prioritizing 'album_key'.")
            album_name = None
            self.config['album_name'] = placeholders['album_name']
            self.save_config()

        self.album_key = album_key
        self.album_api_uri = album_api_uri
        self.album_name = album_name
        self.folder_name = folder_name
        return True

    def _make_api_request(self, method, url, full_url=None, **kwargs):
        """Helper: Makes API requests, includes raw text debug log."""
        if not self.auth_session:
            logger.error("No SmugMug auth session.")
            return None, None

        request_url = full_url
        if not request_url:
            if url and not url.startswith('http'):
                request_url = self.SMUGMUG_API_BASE_URL + url
            else:
                request_url = url

        if not request_url:
            logger.error("No valid URL for API request.")
            return None, None

        headers = kwargs.pop('headers', {})
        headers['Accept'] = 'application/json'

        try:
            response = self.auth_session.request(method, request_url, headers=headers, **kwargs)
            # Debug log with raw response text
            logger.debug(f"SmugMug Response ({method} {request_url}) - Status: {response.status_code}, Text: {response.text[:500]}...")
            response.raise_for_status()

            try:
                 data = response.json()
                 if isinstance(data, dict) and data.get('stat') == 'fail':
                      msg = data.get('message', 'Unknown SmugMug API error')
                      code = data.get('code', 'N/A')
                      logger.error(f"SmugMug API call failed ({method} {request_url}): Code {code} - {msg}")
                      return response, None
                 return response, data
            except json.JSONDecodeError:
                 logger.error(f"Failed decode JSON ({method} {request_url}). Status: {response.status_code}")
                 return response, None

        except requests.exceptions.RequestException as e:
            logger.error(f"SmugMug request failed ({method} {request_url}): {e}", exc_info=True)
            return None, None
        except Exception as e:
            logger.error(f"Unexpected SM request error ({method} {request_url}): {e}", exc_info=True)
            return None, None

    def get_user_endpoint(self):
        """Gets authenticated user info and node URI."""
        if self.user_uri:
            return self.user_uri, {"NickName": self.username}

        logger.info("Fetching SmugMug authenticated user info...")
        _, data = self._make_api_request('GET', '/api/v2!authuser?_expand=Node')
        if data and 'Response' in data and 'User' in data['Response']:
            user_info = data['Response']['User']
            logger.debug(f"!authuser user_info received: {user_info}")
            self.username = user_info.get('NickName', 'UnknownUser')
            node_uri_info = user_info.get('Uris', {}).get('Node', {})
            user_node_uri_string = node_uri_info.get('Uri')
            if user_node_uri_string:
                self.user_uri = user_node_uri_string
                logger.info(f"Found User Node URI: {self.user_uri}")
                return self.user_uri, user_info
            else:
                logger.error("Could not find Node URI in !authuser response.")
                return None, None
        else:
            logger.error(f"Failed getting user info. Raw: {data}")
            return None, None

    def upload_media(self, album_api_uri, file_path, filename, mime_type):
        """Uploads media, handles cleanup, raises SmugMugAlbumFullError."""
        if not self.auth_session:
            logger.error(f"Upload '{filename}': Not auth.")
            return False
        if not os.path.exists(file_path):
            logger.error(f"Upload '{filename}': Not found {file_path}")
            return False

        headers = {
            'X-Smug-ResponseType': 'JSON',
            'X-Smug-Version': 'v2',
            'X-Smug-AlbumUri': album_api_uri,
            'X-Smug-FileName': filename,
            'Content-Type': mime_type,
            'Content-Length': str(os.path.getsize(file_path)),
        }
        logger.info(f"Uploading '{filename}' ({headers['Content-Length']} bytes) to {album_api_uri}...")
        try:
            with open(file_path, 'rb') as f:
                response = self.auth_session.post(self.SMUGMUG_UPLOAD_URL, headers=headers, data=f)
            try:
                response_data = response.json()
                logger.debug(f"Upload Response JSON: {response_data}")
                if response_data.get('stat') == 'fail':
                    error_code = response_data.get('code')
                    error_message = response_data.get('message', 'Unknown')
                    logger.error(f"Upload failed '{filename}': Code {error_code} - {error_message}")
                    if error_code == 63:
                        raise SmugMugAlbumFullError(error_message)
                    return False
                elif response_data.get('stat') == 'ok' and ('Image' in response_data or 'Video' in response_data):
                    response.raise_for_status()
                    logger.info(f"Uploaded '{filename}'.")
                    return True
                else:
                    logger.error(f"Unexpected upload response '{filename}': {response_data}")
                    return False
            except json.JSONDecodeError:
                response.raise_for_status()
                logger.error(f"Failed decode JSON '{filename}'. Status: {response.status_code}, Text: {response.text[:200]}...")
                return False
            except SmugMugAlbumFullError:
                raise
            except Exception as e:
                logger.error(f"Error processing upload response '{filename}': {e}", exc_info=True)
                return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error uploading '{filename}': {e}", exc_info=True)
            return False
        except SmugMugAlbumFullError:
            raise
        except Exception as e:
            logger.error(f"Error uploading '{filename}': {e}", exc_info=True)
            return False
        finally:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.debug(f"Cleaned temp file: {file_path}")
                except OSError as e:
                    logger.warning(f"Failed remove temp {file_path}: {e}")

    def check_media_exists(self, album_key, filename, mime_type, file_hash=None):
        """Checks if media exists via hash (images) or filename (videos)."""
        if not self.auth_session:
            logger.error("Check media: Not auth.")
            return False
        if not album_key:
            logger.error("Check media: Album Key missing.")
            return False

        is_video = mime_type.startswith('video/')
        check_method = "MD5 hash" if not is_video and file_hash else "filename"
        logger.debug(f"Checking album {album_key} for '{filename}' via {check_method}...")

        album_media_uri = f'/api/v2/album/{album_key}!images'
        params = {'count': 100}
        search_uri = album_media_uri # Default to searching within album images

        if is_video:
            params['_filter'] = 'FileName'
            params['_filteruri'] = album_media_uri
            search_uri = '/api/v2/image!search'
        elif file_hash:
            params['_filter'] = 'ArchivedMD5'
            params['_filtervalue'] = file_hash
            # search_uri remains album_media_uri
        else: # Image without hash (e.g., HEIC)
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
                 if not media_list:
                     break # No more items found

                 for media in media_list:
                      if is_video:
                           if media.get('FileName', '').lower() == filename.lower():
                                logger.info(f"Found '{filename}' in {album_key} by filename.")
                                return True
                      elif media.get('ArchivedMD5') == file_hash:
                           logger.info(f"Found '{filename}' in {album_key} by MD5.")
                           return True

                 paging = data['Response'].get('Pages')
                 if paging and paging.get('NextPage'):
                      try:
                           next_page_start = int(paging['NextPage'].split('start=')[1].split('&')[0])
                           logger.debug(f"Paginating check, next: {next_page_start}")
                      except (IndexError, ValueError) as e:
                           logger.warning(f"Cannot parse NextPage URI: {paging['NextPage']} - {e}")
                           break
                 else:
                      break # No more pages
            elif data and 'Response' in data and 'Image' not in data['Response']:
                 break # No 'Image' key means no results found
            else:
                 logger.warning(f"Failed retrieve media from {album_key}.")
                 return False # Treat API errors as potentially not found

        logger.debug(f"Media '{filename}' not found in {album_key} via {check_method}.")
        return False

    def get_or_create_folder(self, parent_node_uri, folder_name):
        """Finds or creates a folder within a parent node using !children."""
        if not parent_node_uri or not folder_name:
            logger.error("Parent node URI/folder name required.")
            return None

        logger.info(f"Checking folder '{folder_name}' under {parent_node_uri} via !children...")
        children_uri_base = f"{parent_node_uri}!children"
        found_folder_uri = None
        next_page_uri = children_uri_base # Start with relative URI

        while next_page_uri:
            current_request_uri = next_page_uri
            # Construct full URL for API call
            if not current_request_uri.startswith(self.SMUGMUG_API_BASE_URL):
                 if current_request_uri.startswith('/'):
                      current_request_uri = self.SMUGMUG_API_BASE_URL + current_request_uri
                 else:
                      logger.warning(f"Bad relative URI: {current_request_uri}. Prepending base URL.")
                      current_request_uri = self.SMUGMUG_API_BASE_URL + '/' + current_request_uri.lstrip('/')

            # Add count parameter
            separator = '&' if '?' in current_request_uri else '?'
            if 'count=' not in current_request_uri:
                current_request_uri += f"{separator}count=100"

            # Make API call
            _, data = self._make_api_request('GET', None, full_url=current_request_uri)

            if data and 'Response' in data and 'Node' in data['Response']:
                child_nodes = data['Response']['Node']
                for node in child_nodes:
                    if node.get('Type') == 'Folder' and node.get('Name', '').lower() == folder_name.lower():
                        found_folder_uri = node.get('Uri')
                        if found_folder_uri:
                            logger.info(f"Found existing folder '{folder_name}': {found_folder_uri}")
                            return found_folder_uri
                        else:
                            logger.warning(f"Folder node '{folder_name}' found but missing URI: {node}")
                            # Continue checking other nodes

                # Handle pagination
                paging = data['Response'].get('Pages')
                if paging and paging.get('NextPage'):
                    next_page_uri = paging['NextPage'] # Get relative URI for next page
                    logger.debug(f"Paginating children, next relative URI: {next_page_uri}")
                else:
                    next_page_uri = None # No more pages
            elif data and 'Response' in data and 'Node' not in data['Response']:
                 logger.debug(f"No 'Node' key in children response for {parent_node_uri}.")
                 next_page_uri = None # No children or empty page
            else:
                logger.warning(f"Failed retrieving children from {parent_node_uri}.")
                return None # Treat API error as fatal

        # --- Folder Not Found ---
        if not found_folder_uri:
            logger.info(f"Folder '{folder_name}' not found. Creating...")
            # Generate URL name
            url_name_base = ''.join(c for c in folder_name if c.isalnum() or c in (' ', '-')).strip().title().replace(' ', '')
            url_name = url_name_base[:50] if url_name_base else f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"
            if not url_name:
                url_name = f"Folder{hashlib.md5(folder_name.encode()).hexdigest()[:8]}"
            if url_name and url_name[0].islower():
                url_name = url_name[0].upper() + url_name[1:]

            # Create folder payload
            create_payload = {
                'Name': folder_name,
                'UrlName': url_name,
                'Type': 'Folder',
                'Privacy': 'Private'
            }
            create_url = f"{parent_node_uri}!children"
            post_response, create_data = self._make_api_request('POST', create_url, json=create_payload)

            # Process creation response
            if create_data and 'Response' in create_data and 'Node' in create_data['Response']:
                new_folder_uri = create_data['Response']['Node'].get('Uri')
                if new_folder_uri:
                    logger.info(f"Folder '{folder_name}' created: {new_folder_uri}")
                    return new_folder_uri
                else:
                    logger.error(f"Folder '{folder_name}' created, response missing URI: {create_data}")
                    return None
            else:
                # Log detailed error on creation failure
                smugmug_error = ""
                if post_response is not None and 400 <= post_response.status_code < 600:
                     try:
                          error_json = post_response.json()
                          smugmug_error = f" (Code: {error_json.get('code','N/A')}, Msg: {error_json.get('message','N/A')})"
                     except Exception:
                          smugmug_error = f" (Status: {post_response.status_code})"
                logger.error(f"Failed creating folder '{folder_name}' under {parent_node_uri}.{smugmug_error}")
                return None

        return None # Fallback

    def get_or_create_album_in_path(self, album_name, folder_path_str):
        """Finds or creates album, handling nested folders (uses !children)."""
        user_node_uri, _ = self.get_user_endpoint()
        if not user_node_uri:
            logger.critical("No User Node URI.")
            return False

        parent_node_uri = user_node_uri
        if folder_path_str:
             folder_names = [name.strip() for name in folder_path_str.split('/') if name.strip()]
             logger.info(f"Ensuring folder path: {'/'.join(folder_names)}")
             for name in folder_names:
                  folder_uri = self.get_or_create_folder(parent_node_uri, name) # Uses !children logic now
                  if not folder_uri:
                       logger.error(f"Failed find/create folder '{name}'.")
                       return False
                  parent_node_uri = folder_uri
             logger.info(f"Folder path ok. Final parent: {parent_node_uri}")
        else:
             logger.info("No folder path specified, using user root.")

        # Find or create album using !children
        logger.info(f"Checking for album '{album_name}' under {parent_node_uri} via !children...")
        children_uri_base = f"{parent_node_uri}!children"
        found_album_uri = None
        found_album_key = None
        next_page_uri = children_uri_base # Start with relative URI

        while next_page_uri:
            current_request_uri = next_page_uri
            # Construct full URL
            if not current_request_uri.startswith(self.SMUGMUG_API_BASE_URL):
                 if current_request_uri.startswith('/'):
                      current_request_uri = self.SMUGMUG_API_BASE_URL + current_request_uri
                 else:
                      current_request_uri = self.SMUGMUG_API_BASE_URL + '/' + current_request_uri.lstrip('/')

            # Add count param
            separator = '&' if '?' in current_request_uri else '?'
            if 'count=' not in current_request_uri:
                current_request_uri += f"{separator}count=100"

            # Make API call
            _, data = self._make_api_request('GET', None, full_url=current_request_uri)

            if data and 'Response' in data and 'Node' in data['Response']:
                child_nodes = data['Response']['Node']
                for node in child_nodes:
                    if node.get('Type') == 'Album' and node.get('Name', '').lower() == album_name.lower():
                        temp_album_api_uri = node.get('Uris', {}).get('Album', {}).get('Uri')
                        if temp_album_api_uri:
                            found_album_uri = temp_album_api_uri
                            found_album_key = found_album_uri.split('/')[-1]
                            break # Exit inner loop once found
                        else:
                            logger.warning(f"Album node '{album_name}' found but missing Album URI: {node}")

                if found_album_uri:
                    break # Exit outer loop if found

                # Handle pagination
                paging = data['Response'].get('Pages')
                if paging and paging.get('NextPage'):
                    next_page_uri = paging['NextPage'] # Get relative URI
                else:
                    next_page_uri = None # No more pages
            elif data and 'Response' in data and 'Node' not in data['Response']:
                 next_page_uri = None # No children or empty page
            else:
                logger.warning(f"Failed retrieving children from {parent_node_uri}.")
                return False # Treat API error as fatal

        # Process result after loop
        if found_album_uri and found_album_key:
             logger.info(f"Found existing album '{album_name}': Key={found_album_key}, URI={found_album_uri}")
             self.album_key = found_album_key
             self.album_api_uri = found_album_uri
             self.album_name = album_name
             return True
        else:
             # Album not found, create it
             logger.info(f"Album '{album_name}' not found. Creating...")
             # Generate URL name
             url_name_base = ''.join(c for c in album_name if c.isalnum() or c in (' ', '-')).strip().title().replace(' ', '')
             url_name = url_name_base[:50] if url_name_base else f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}"
             if not url_name:
                 url_name = f"Album{hashlib.md5(album_name.encode()).hexdigest()[:8]}"
             if url_name and not url_name[0].isupper():
                 url_name = url_name[0].upper() + url_name[1:]

             # Create payload
             create_payload = {
                 'Name': album_name,
                 'UrlName': url_name,
                 'Type': 'Album',
                 'Privacy': 'Private'
             }
             create_url = f"{parent_node_uri}!children"
             post_response, create_data = self._make_api_request('POST', create_url, json=create_payload)

             # Process creation response
             if create_data and 'Response' in create_data and 'Node' in create_data['Response']:
                  new_node_data = create_data['Response']['Node']
                  self.album_api_uri = new_node_data.get('Uris', {}).get('Album', {}).get('Uri')
                  self.album_key = self.album_api_uri.split('/')[-1] if self.album_api_uri else None
                  if self.album_api_uri and self.album_key:
                       logger.info(f"Album '{album_name}' created: Key={self.album_key}, URI={self.album_api_uri}")
                       self.album_name = album_name
                       return True
                  else:
                       logger.error(f"Album '{album_name}' created, but response missing Key/URI: {create_data}")
                       return False
             else:
                  # Log detailed error on creation failure
                  smugmug_error = ""
                  if post_response is not None and 400 <= post_response.status_code < 600:
                       try:
                           error_json = post_response.json()
                           smugmug_error = f" (Code: {error_json.get('code','N/A')}, Msg: {error_json.get('message','N/A')})"
                       except Exception:
                           smugmug_error = f" (Status: {post_response.status_code})"
                  logger.error(f"Failed creating album '{album_name}' under {parent_node_uri}.{smugmug_error}")
                  return False

    @staticmethod
    def calculate_file_hash(file_path, hash_algorithm='md5'):
        """Calculates the hash of a file (default: md5)."""
        hasher = None
        if hash_algorithm.lower() == 'md5':
            hasher = hashlib.md5()
        elif hash_algorithm.lower() == 'sha256':
            hasher = hashlib.sha256()
        else:
            logger.error(f"Unsupported hash algo: {hash_algorithm}")
            return None

        try:
            with open(file_path, 'rb') as afile:
                buf_size = 65536 # 64KB chunks
                while True:
                    data = afile.read(buf_size)
                    if not data:
                        break
                    hasher.update(data)
            return hasher.hexdigest()
        except FileNotFoundError:
            logger.error(f"File not found for hash: {file_path}")
            return None
        except Exception as e:
            logger.error(f"Error hashing {file_path}: {e}", exc_info=True)
            return None

