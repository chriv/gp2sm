# SmugMug Module
#
# This module encapsulates all SmugMug-related functionality for the Google Photos to SmugMug Transfer Script.
#
# Attribution:
# - Core logic and structure generated with assistance from Google Gemini AI.
# - SmugMug upload logic inspired by/adapted from SkiTheSlicer's work:
#   https://github.com/SkiTheSlicer/smugmug-api-v2-upload
#

import hashlib
import json
import logging
# Standard library imports
import os

# Third-party imports
import requests
from requests_oauthlib import OAuth1Session


class SmugMug:
    """Class encapsulating all SmugMug-related functionality."""

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

        # Try to load config and authenticate during initialization
        if isinstance(config_file, dict):
            # If a config dictionary was passed directly
            self.config = config_file
            self.authenticate()
        else:
            # If a config file path was passed
            self.load_config()
            if self.config:
                self.authenticate()

    def is_authenticated(self):
        """Returns True if authenticated with SmugMug, False otherwise."""
        # Basic authentication requires auth_session
        if self.auth_session is None:
            return False

        # Either album_key and album_api_uri OR album_name must be set
        if (self.album_key is not None and self.album_api_uri is not None) or self.album_name is not None:
            return True

        return False

    def load_config(self):
        """Loads SmugMug API configuration from a JSON file."""
        try:
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
                return self.config
        except FileNotFoundError:
            logging.error(f"SmugMug configuration file not found: {self.config_file}")
            return None
        except json.JSONDecodeError:
            logging.error(f"Error decoding JSON from SmugMug configuration file: {self.config_file}")
            return None

    def save_config(self):
        """Saves SmugMug API configuration to a JSON file."""
        if not self.config:
            logging.error("No SmugMug configuration to save.")
            return False

        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
                logging.info(f"SmugMug configuration saved to {self.config_file}")
                return True
        except IOError as e:
            logging.error(f"Error saving SmugMug configuration to {self.config_file}: {e}")
            return False

    def obtain_oauth_tokens(self, api_key, api_secret):
        """Obtains SmugMug OAuth tokens using the OAuth 1.0a flow."""
        request_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getRequestToken'
        authorize_url = 'https://secure.smugmug.com/services/oauth/1.0a/authorize'
        access_token_url = 'https://secure.smugmug.com/services/oauth/1.0a/getAccessToken'

        smugmug = OAuth1Session(api_key, client_secret=api_secret, callback_uri='oob')
        logging.info("Fetching request token from SmugMug...")
        try:
            fetch_response = smugmug.fetch_request_token(request_token_url)
            if fetch_response:
                url = smugmug.authorization_url(authorize_url)
                print(f"Please open the following URL in your browser and authorize the application:\n{url}")
                verifier = input("Enter the verifier code you received after authorizing: ")
                token_response = smugmug.fetch_access_token(access_token_url, verifier=verifier)
                if token_response:
                    return smugmug.token.get('oauth_token'), smugmug.token.get('oauth_token_secret')
                else:
                    logging.error("Failed to fetch access token from SmugMug.")
                    return None, None
            else:
                logging.error("Failed to fetch request token from SmugMug. Check API key/secret or network.")
                return None, None
        except Exception as e:
            logging.error(f"An error occurred during SmugMug OAuth request token or access token fetch: {e}")
            return None, None

    def authenticate(self):
        """
        Authenticates with the SmugMug API using OAuth 1.0a.
        Returns True if authentication is successful, False otherwise.
        """
        if not self.config:
            self.config = self.load_config()

        if not self.config:
            logging.error("SmugMug configuration not available for authentication.")
            return False

        api_key = self.config.get('api_key')
        api_secret = self.config.get('api_secret')
        oauth_token = self.config.get('oauth_token')
        oauth_token_secret = self.config.get('oauth_token_secret')

        if not api_key or not api_secret:
            logging.error("SmugMug API Key and Secret must be configured in smugmug_config.json.")
            return False

        # Check if tokens are missing or placeholder values
        if not oauth_token or not oauth_token_secret or oauth_token == "YOUR_SMUGMUG_OAUTH_TOKEN":
            logging.info("SmugMug OAuth tokens not found or invalid in config. Initiating authorization flow...")
            new_oauth_token, new_oauth_token_secret = self.obtain_oauth_tokens(api_key, api_secret)
            if new_oauth_token and new_oauth_token_secret:
                self.config['oauth_token'] = new_oauth_token
                self.config['oauth_token_secret'] = new_oauth_token_secret
                self.save_config()
                oauth_token = new_oauth_token
                oauth_token_secret = new_oauth_token_secret
            else:
                logging.error("Failed to obtain SmugMug OAuth tokens. Please check the logs and try again.")
                return False

        # If we have tokens (either loaded or newly obtained), create the session
        if oauth_token and oauth_token_secret and oauth_token != "YOUR_SMUGMUG_OAUTH_TOKEN":
            self.auth_session = OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=oauth_token,
                                              resource_owner_secret=oauth_token_secret)
            logging.info("SmugMug API authentication configured.")

            # Set album details from config
            self.album_key = self.config.get('album_key')
            self.album_api_uri = self.config.get('album_api_uri')
            self.album_name = self.config.get('album_name')
            self.folder_name = self.config.get('folder_name')

            # Handle placeholder values and prioritize album_name or album_key
            if self.album_name and self.album_name.strip() != '' and self.album_name != 'YOUR_SMUGMUG_ALBUM_NAME':
                logging.info(f"Using album name from config file: '{self.album_name}'")
                # Clear key and uri as name takes precedence
                self.album_key = None
                self.album_api_uri = None
            elif self.album_key and self.album_key.strip() != '' and self.album_key != 'TARGET_SMUGMUG_ALBUM_KEY':
                logging.info(f"Using album key from config file: '{self.album_key}'")
                # If album_api_uri is missing or placeholder, generate it
                if not self.album_api_uri or self.album_api_uri == '/api/v2/album/TARGET_SMUGMUG_ALBUM_KEY':
                    self.album_api_uri = f'/api/v2/album/{self.album_key}'
                    logging.info(f"Generated album_api_uri from album_key: '{self.album_api_uri}'")
                # Clear name as key takes precedence
                self.album_name = None
            else:
                # If neither album_name nor album_key is valid, log an error and return False
                logging.error(
                    "SmugMug configuration must include either a valid 'album_key' and 'album_api_uri' OR a valid 'album_name'.")
                logging.error(
                    "Please configure these in smugmug_config.json or use the --smugmug-album command line argument.")
                return False

            # If we have folder_name, log it
            if self.folder_name:
                logging.info(f"Using folder name from config file: '{self.folder_name}'")

            return True
        else:
            logging.error("SmugMug OAuth tokens are still missing or invalid after attempted authorization.")
            return False

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

        if not album_key:
            album_key = self.album_key
            if not album_key:
                logging.warning("No SmugMug Album Key provided. Cannot check for media existence.")
                return False

        is_video = mime_type.startswith('video/')
        # MD5 is required for images per SmugMug API for existence checks
        if not is_video and not file_hash:
            logging.warning(f"Image file '{filename}' requires an MD5 hash for existence check, but none was provided.")
            return False  # Cannot check image without hash

        # Construct the API URL to list images in the album
        album_details_url = f"https://api.smugmug.com/api/v2/album/{album_key}"
        headers = {'Accept': 'application/json'}

        try:
            # First, get album details to find the Images URI
            album_response = self.auth_session.get(album_details_url, headers=headers)
            album_response.raise_for_status()  # Raise for HTTP errors
            album_data = album_response.json()

            # Navigate through the response to find the Images URI
            images_uri = album_data.get('Response', {}).get('Album', {}).get('Uris', {}).get('AlbumImages', {}).get(
                'Uri')

            if not images_uri:
                logging.error(f"Could not find Images URI for SmugMug album key {album_key}. Cannot check existence.")
                return False

            images_list_url = f"https://api.smugmug.com{images_uri}"
            next_page_url = images_list_url

            # Now, iterate through the album images using the found URI
            while next_page_url:
                logging.debug(f"Checking SmugMug images page: {next_page_url}")
                response = self.auth_session.get(next_page_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                # SmugMug API v2 lists images under 'Response' -> 'AlbumImage' for album image list
                if 'Response' in data and 'AlbumImage' in data['Response']:
                    for item in data['Response']['AlbumImage']:
                        item_filename = item.get('FileName')
                        item_type = item.get('Type')  # SmugMug might distinguish 'Image' and 'Video' types

                        # Check based on media type
                        if is_video:
                            # For videos, compare filenames
                            if item_filename and filename.lower() == item_filename.lower() and (
                                    item_type is None or item_type == 'Video'):
                                return True
                        else:
                            # For images, compare MD5 hashes
                            smugmug_md5 = item.get('ArchivedMD5')  # This field stores the MD5 hash
                            if file_hash and smugmug_md5 and file_hash.lower() == smugmug_md5.lower() and (
                                    item_type is None or item_type == 'Image'):
                                logging.info(f"Image '{filename}' found on SmugMug with matching MD5 hash: {file_hash}")
                                return True  # Exact match found

                # Pagination logic
                pages_info = data.get('Response', {}).get('Pages')
                if pages_info and 'NextPage' in pages_info:
                    # Construct the full URL for the next page
                    next_page_url = f"https://api.smugmug.com{pages_info['NextPage']}"
                else:
                    next_page_url = None  # No more pages

            # If the loop finishes without finding the item, it doesn't exist
            logging.debug(f"Media '{filename}' not found in SmugMug album {album_key} after checking all pages.")
            return False

        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking for media existence on SmugMug (filename: {filename}): {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return False
        except Exception as e:
            logging.error(f"An unexpected error occurred during SmugMug existence check for '{filename}': {e}")
            return False

    def upload_media(self, album_api_uri, file_path, filename, mime_type):
        """
        Upload media file to specified Album API URI.
        Returns True if successful, False otherwise.
        """
        if not self.is_authenticated():
            logging.error("Not authenticated with SmugMug. Cannot upload media.")
            return False

        if not album_api_uri:
            album_api_uri = self.album_api_uri
            if not album_api_uri:
                logging.error("No SmugMug Album API URI provided. Cannot upload media.")
                return False

        if not os.path.exists(file_path):
            logging.error(f"Upload failed: Temporary file not found at {file_path}")
            return False

        try:
            # Calculate MD5 hash for the uploaded file content as required by SmugMug
            file_md5 = self.calculate_file_hash(file_path, hash_algorithm='md5')
            if not file_md5:
                logging.error(f"Failed to calculate MD5 hash for {filename}. Cannot upload.")
                return False

            # Read file content for upload
            with open(file_path, 'rb') as media_file:
                media_data = media_file.read()

            headers = {
                'Accept': 'application/json',
                'Content-Length': str(len(media_data)),
                'Content-MD5': file_md5,
                'Content-Type': mime_type,
                'X-Smug-AlbumUri': album_api_uri,
                'X-Smug-FileName': filename,
                'X-Smug-ResponseType': 'JSON',
                'X-Smug-Version': 'v2',
            }

            logging.info(f"Attempting upload for '{filename}' to SmugMug album URI: {album_api_uri}")

            # The upload URL is fixed: https://upload.smugmug.com/
            response = self.auth_session.post('https://upload.smugmug.com/', headers=headers, data=media_data)
            response.raise_for_status()
            upload_data = response.json()

            # Check the response structure for success indication
            if upload_data.get('stat') == 'ok' and 'Image' in upload_data:
                image_info = upload_data.get('Image', {})
                status_url = image_info.get('StatusURL')
                image_url = image_info.get('URL')
                logging.info(
                    f"Successfully initiated upload for '{filename}'. SmugMug Status URL: {status_url}, Final URL (approx): {image_url}")
                return True
            else:
                logging.error(f"Failed to upload '{filename}' to SmugMug. Response: {upload_data}")
                return False

        except requests.exceptions.RequestException as e:
            logging.error(f"Error uploading {filename} to SmugMug: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                logging.error(f"HTTP Status Code: {e.response.status_code}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return False
        except Exception as e:
            logging.error(f"An unexpected error occurred during upload of '{filename}': {e}")
            return False

    def get_user_endpoint(self):
        """
        Gets the user's endpoint from the SmugMug API.
        Returns the username and user data if successful, None otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot get user endpoint.")
            return None, None

        try:
            # Get the user data from the SmugMug API
            user_url = "https://api.smugmug.com/api/v2!authuser"
            headers = {'Accept': 'application/json'}
            response = self.auth_session.get(user_url, headers=headers)
            response.raise_for_status()
            user_data = response.json()

            # Extract the username from the user data
            user = user_data.get('Response', {}).get('User', {})
            username = user.get('NickName')

            if not username:
                logging.error("Could not find username in SmugMug API response.")
                return None, None

            logging.info(f"Found SmugMug username: {username}")
            self.username = username

            return username, user_data
        except requests.exceptions.RequestException as e:
            logging.error(f"Error getting user endpoint from SmugMug: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return None, None
        except Exception as e:
            logging.error(f"An unexpected error occurred while getting user endpoint from SmugMug: {e}")
            return None, None

    def list_albums(self):
        """
        Lists all available albums from the user's SmugMug account.
        Returns a list of albums if successful, empty list otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot list albums.")
            return []

        # Get the username if we don't have it yet
        if not self.username:
            username, _ = self.get_user_endpoint()
            if not username:
                logging.error("Could not get username. Cannot list albums.")
                return []

        try:
            # Try direct album search using username
            endpoint = f"/api/v2/user/{self.username}!albums"
            logging.info(f"Searching for album using endpoint: {endpoint}")

            headers = {'Accept': 'application/json'}
            url = f"https://api.smugmug.com{endpoint}"
            response = self.auth_session.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            albums = []
            if 'Response' in data and 'Album' in data['Response']:
                albums = data['Response']['Album']
                logging.info(f"Found {len(albums)} albums in direct search")
                return albums

            # If we didn't find any albums, try using the Node URI
            username, user_data = self.get_user_endpoint()
            if not username or not user_data:
                logging.error("Could not get user data. Cannot list albums.")
                return []

            # Try to find Albums URI in user data
            albums_uri = None
            uris = user_data.get('Response', {}).get('User', {}).get('Uris', {})

            # Check for UserAlbums URI
            if 'UserAlbums' in uris:
                albums_uri = uris['UserAlbums'].get('Uri')

            if not albums_uri:
                # Log available URIs for debugging
                available_uris = list(uris.keys()) if uris else []
                logging.error("Could not find Albums URI in SmugMug API response.")
                logging.error(f"Available URIs in user data: {available_uris}")

                # Try using Node URI
                node_uri = uris.get('Node', {}).get('Uri')
                if node_uri:
                    logging.info(f"Trying to use Node URI to find albums: {node_uri}")
                    node_url = f"https://api.smugmug.com{node_uri}"
                    node_response = self.auth_session.get(node_url, headers=headers)
                    node_data = node_response.json()

                    # Check if Node has an Albums URI
                    node_uris = node_data.get('Response', {}).get('Node', {}).get('Uris', {})
                    if 'NodeAlbums' in node_uris:
                        albums_uri = node_uris['NodeAlbums'].get('Uri')

                # Try using Folder URI
                folder_uri = uris.get('Folder', {}).get('Uri')
                if folder_uri and not albums_uri:
                    logging.info(f"Trying to use Folder URI to find albums: {folder_uri}")
                    folder_url = f"https://api.smugmug.com{folder_uri}"
                    folder_response = self.auth_session.get(folder_url, headers=headers)
                    folder_data = folder_response.json()

                    # Check if Folder has an Albums URI
                    folder_uris = folder_data.get('Response', {}).get('Folder', {}).get('Uris', {})
                    if 'FolderAlbums' in folder_uris:
                        albums_uri = folder_uris['FolderAlbums'].get('Uri')

            if not albums_uri:
                logging.error("No albums found in SmugMug account.")
                return []

            # Get albums using the found URI
            albums_url = f"https://api.smugmug.com{albums_uri}"
            albums_response = self.auth_session.get(albums_url, headers=headers)
            albums_response.raise_for_status()
            albums_data = albums_response.json()

            if 'Response' in albums_data and 'Album' in albums_data['Response']:
                albums = albums_data['Response']['Album']
                logging.info(f"Found {len(albums)} albums")
                return albums
            else:
                logging.error("No albums found in SmugMug account.")
                return []


        except requests.exceptions.RequestException as e:
            logging.error(f"Error listing albums from SmugMug: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return []
        except Exception as e:
            logging.error(f"An unexpected error occurred while listing albums from SmugMug: {e}")
            return []

    def select_album_by_name(self, album_name):
        """
        Selects an album by name and sets the album_key and album_api_uri.
        Returns True if successful, False otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot select album.")
            return False

        if not album_name:
            logging.error("Album name not provided. Cannot select album.")
            return False

        # Get all albums
        albums = self.list_albums()
        if not albums:
            logging.error("No albums found. Cannot select album.")
            return False

        # Find the album with the matching name (case-insensitive)
        for album in albums:
            if album.get('Name', '').lower() == album_name.lower():
                self.album_key = album.get('AlbumKey')
                self.album_api_uri = album.get('Uri')

                if not self.album_key or not self.album_api_uri:
                    logging.error(f"Album '{album_name}' found, but missing AlbumKey or Uri.")
                    return False

                logging.info(f"Selected album '{album_name}' with key {self.album_key}")
                return True

        logging.info(f"Album '{album_name}' not found in SmugMug account.")
        return False

    def create_album(self, album_name, folder_uri=None):
        """
        Creates a new album with the specified name and settings.
        If folder_uri is provided, creates the album under that folder node.
        Returns the album key and album URI if successful, None otherwise.
        Also saves the obtained album_key and album_api_uri to the config file.
        Includes validation and correction of album attributes after creation.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot create album.")
            return None, None

        if not album_name:
            logging.error("Album name not provided. Cannot create album.")
            return None, None

        # Get the username if we don't have it yet
        if not self.username:
            username, _ = self.get_user_endpoint()
            if not username:
                logging.error("Could not get username. Cannot create album.")
                return None, None

        try:
            # Determine the correct URL for album creation.
            # If a folder_uri is provided, post to that folder's node URI!children.
            # Otherwise, default to creating under the user's root node by POSTing to root node URI!children.
            if folder_uri:
                # When creating under a node, post to the node's URI!children endpoint
                creation_url = f"https://api.smugmug.com{folder_uri}!children"
                logging.info(f"Creating album '{album_name}' under folder node using URL: {creation_url}")
            else:
                # Default to creating under the user's root node if no folder_uri is given
                username, user_data = self.get_user_endpoint()
                if not username or not user_data:
                    logging.error("Could not get user data for root node. Cannot create album.")
                    return None, None

                uris = user_data.get('Response', {}).get('User', {}).get('Uris', {})
                root_node_uri = uris.get('Node', {}).get('Uri')

                if not root_node_uri:
                    logging.error("Could not find root Node URI. Cannot create album.")
                    return None, None

                # When creating directly under the root node, POST to the root node's children endpoint
                creation_url = f"https://api.smugmug.com{root_node_uri}!children"
                logging.info(f"Creating album '{album_name}' under root node using URL: {creation_url}")

            # Prepare the album creation request
            headers = {
                'Accept': 'application/json',
                # Use form-urlencoded content type for node creation
                'Content-Type': 'application/x-www-form-urlencoded'
            }

            # Set album settings with desired defaults
            # Use the simplified data for creation under a node, aligning with working example
            album_data = {
                'Name': album_name,
                'UrlName': album_name.title().replace(' ', ''),  # First character needs to be uppercase
                'Type': 'Album',  # Explicitly set type to Album
                'Privacy': 'Private',  # Set Privacy to Private (Only Me) as requested
                'LargestSize': 'Original',  # Set Maximum Display Size to Original
                'Protected': False,  # Set Right-Click Message to Off
                'SmugSearchable': 'No',  # Set SmugMug Searchable to No
                'WorldSearchable': 'No',  # Set Web Searchable to No
                'AllowDownloads': True  # Set Allow Downloads to On
            }
            # Removed: logging.info("Using simplified album_data for creation under node with Privacy set to Private.")

            # Create the album by POSTing to the determined URL
            # Use 'data' parameter instead of 'json' for form-urlencoded data
            response = self.auth_session.post(creation_url, headers=headers, data=album_data)
            response.raise_for_status()
            data = response.json()

            # Extract the album key and URI from the response
            # The response to a create in node request returns the newly created node.
            # We need to extract album details from this node response.
            album_key = None
            album_uri = None

            node_response = data.get('Response', {}).get('Node', {})
            if node_response and node_response.get('Type') == 'Album':
                logging.info("API response contains a Node object of type Album.")
                # The created album's details should be linked from the created node's Uris
                node_uris = node_response.get('Uris', {})
                # The API provides the Album URI under the 'Album' key in the node's Uris
                album_uri_data = node_uris.get('Album', {})  # Look specifically for 'Album' key in Uris

                if album_uri_data:
                    album_uri = album_uri_data.get('Uri')
                    # The AlbumKey is also available directly on the returned Node object
                    # OR it is the last segment of the Album URI
                    album_key = node_response.get('AlbumKey')  # Try getting from Node object first
                    if not album_key and album_uri:  # If not on Node object, try splitting URI
                        album_key = album_uri.split('/')[-1]

                    if not album_uri or not album_key:
                        logging.error(
                            f"Created Album Node, but missing Album URI ('{album_uri}') or Album Key ('{album_key}') in response.")
                        logging.error(f"Node Response: {json.dumps(node_response, indent=2)}")
                        # Set to None to indicate failure in getting details
                        album_key = None
                        album_uri = None
                    else:
                        logging.info(
                            f"Successfully extracted Album URI '{album_uri}' and Album Key '{album_key}' from created Node response.")
                else:
                    logging.error("Created Album Node, but missing 'Album' URI data in node Uris.")
                    logging.error(f"Node Response: {json.dumps(node_response, indent=2)}")
            else:
                logging.error("API response did not contain a Node object of type Album as expected.")
                logging.error(f"Full API Response: {json.dumps(data, indent=2)}")

            # Final check if we successfully got both key and uri
            if album_key and album_uri:
                # Set the album key and URI attributes
                self.album_key = album_key
                self.album_api_uri = album_uri

                # *** Save album_key and album_api_uri to config file ***
                logging.info("Attempting to save album_key and album_api_uri to config file.")
                if self.config is not None:  # Check if config object exists
                    # Save directly to the root of the config dictionary
                    self.config['album_key'] = album_key
                    self.config['album_api_uri'] = album_uri
                    self.save_config()
                    logging.info("Saved album_key and album_api_uri to config file.")
                else:
                    logging.warning("Config object is None. Cannot save album details to config.")

                logging.info(f"Created album '{album_name}' with key {album_key}")

                # *** Validate and correct album attributes after creation ***
                self.validate_and_correct_album_attributes(album_uri)

                return album_key, album_uri
            else:
                logging.error(f"Album '{album_name}' created, but missing AlbumKey or Uri in response.")
                # The Node Response was already logged above if available
                return None, None


        except requests.exceptions.RequestException as e:
            logging.error(f"Error creating album '{album_name}' on SmugMug: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                logging.error(f"HTTP Status Code: {e.response.status_code}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return None, None
        except Exception as e:
            logging.error(f"An unexpected error occurred while creating album '{album_name}' on SmugMug: {e}")
            return None, None

    def validate_and_correct_album_attributes(self, album_uri):
        """
        Validates and corrects the attributes of a newly created album.
        Fetches current attributes and sends a PATCH request for any discrepancies.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot validate/correct album attributes.")
            return

        if not album_uri:
            logging.error("Album URI not provided. Cannot validate/correct album attributes.")
            return

        logging.info(f"Validating and correcting attributes for album at URI: {album_uri}")

        # Define the desired attributes and their target values
        desired_attributes = {
            'LargestSize': 'Original',
            'Protected': False,
            'SmugSearchable': 'No',
            'WorldSearchable': 'No',
            'AllowDownloads': True,
            'Privacy': 'Private'
        }

        try:
            # Fetch the current album details
            headers = {'Accept': 'application/json'}
            response = self.auth_session.get(f"https://api.smugmug.com{album_uri}", headers=headers)
            response.raise_for_status()
            current_album_data = response.json().get('Response', {}).get('Album', {})

            if not current_album_data:
                logging.error(f"Could not fetch current album data for validation from {album_uri}.")
                return

            # Check for discrepancies and build the patch payload
            patch_payload = {}
            logging.debug("Checking album attributes:")
            for attr, desired_value in desired_attributes.items():
                current_value = current_album_data.get(attr)
                logging.debug(f"  {attr}: Current = {current_value}, Desired = {desired_value}")
                # Special handling for boolean values which might be represented differently
                if isinstance(desired_value, bool) and isinstance(current_value, int):
                    # Compare boolean desired value to integer current value (0 or 1)
                    if desired_value != bool(current_value):
                        logging.warning(
                            f"  Attribute '{attr}' has unexpected value: Current = {current_value}, Desired = {desired_value}. Adding to patch payload.")
                        patch_payload[attr] = desired_value
                elif current_value != desired_value:
                    logging.warning(
                        f"  Attribute '{attr}' has unexpected value: Current = {current_value}, Desired = {desired_value}. Adding to patch payload.")
                    patch_payload[attr] = desired_value

            # If there are discrepancies, send a PATCH request to correct them
            if patch_payload:
                logging.info(f"Discrepancies found. Sending PATCH request to correct album attributes for {album_uri}.")
                patch_headers = {
                    'Accept': 'application/json',
                    # *** CHANGE: Use application/json for PATCH requests to Album URI ***
                    'Content-Type': 'application/json'
                }
                # *** CHANGE: Use 'json' parameter instead of 'data' for JSON payload ***
                patch_response = self.auth_session.patch(f"https://api.smugmug.com{album_uri}", headers=patch_headers,
                                                         json=patch_payload)
                patch_response.raise_for_status()
                logging.info(f"Successfully corrected album attributes for {album_uri}.")
            else:
                logging.info(f"Album attributes for {album_uri} are already as desired. No correction needed.")

        except requests.exceptions.RequestException as e:
            logging.error(f"Error validating/correcting album attributes for {album_uri}: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                logging.error(f"HTTP Status Code: {e.response.status_code}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
        except Exception as e:
            logging.error(
                f"An unexpected error occurred during album attribute validation/correction for {album_uri}: {e}")

    def get_or_create_album(self, album_name, folder_uri=None):
        """
        Gets an existing album by name or creates a new one if it doesn't exist.
        Returns True if successful, False otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot get or create album.")
            return False

        if not album_name:
            logging.error("Album name not provided. Cannot get or create album.")
            return False

        logging.info(f"Attempting to find or create album: '{album_name}'")

        # First, try to select the album by name
        if self.select_album_by_name(album_name):
            return True

        # If the album doesn't exist, create it
        logging.info(f"Album '{album_name}' not found. Creating a new album.")
        album_key, album_uri = self.create_album(album_name, folder_uri)

        if album_key and album_uri:
            self.album_key = album_key
            self.album_api_uri = album_uri
            return True

        logging.error(f"Failed to select or create SmugMug album '{album_name}'.")
        return False

    def list_folders(self):
        """
        Lists top-level folders from the user's SmugMug account by getting root node children.
        Returns a list of folder dictionaries if successful, empty list otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot list folders.")
            return []

        try:
            # Get the user's root node URI
            username, user_data = self.get_user_endpoint()
            if not username or not user_data:
                logging.error("Could not get user data. Cannot list folders.")
                return []

            uris = user_data.get('Response', {}).get('User', {}).get('Uris', {})
            root_node_uri = uris.get('Node', {}).get('Uri')

            if not root_node_uri:
                logging.error("Could not find root Node URI in SmugMug API response. Cannot list folders.")
                return []

            # Get the children of the root node (this includes top-level folders and albums)
            children_url = f"https://api.smugmug.com{root_node_uri}!children"
            logging.info(f"Attempting to list children of root node at: {children_url}")

            headers = {'Accept': 'application/json'}
            response = self.auth_session.get(children_url, headers=headers)
            response.raise_for_status()
            data = response.json()

            # The list of child nodes (folders, albums, pages) is typically under 'Response' -> 'Node'
            nodes = data.get('Response', {}).get('Node', [])

            # Filter the nodes to get only the folders
            folders = [node for node in nodes if isinstance(node, dict) and node.get('Type') == 'Folder']

            logging.info(f"Found {len(folders)} top-level folders under the root node.")

            # The list of folders returned here will be used by select_folder_by_name

            return folders

        except requests.exceptions.RequestException as e:
            logging.error(f"Error listing folders from SmugMug: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                logging.error(f"HTTP Status Code: {e.response.status_code}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return []
        except Exception as e:
            logging.error(f"An unexpected error occurred while listing folders from SmugMug: {e}")
            return []

    def create_folder(self, folder_name):
        """
        Creates a new folder with the specified name under the user's root node.
        Returns the folder URI if successful, None otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot create folder.")
            return None

        if not folder_name:
            logging.error("Folder name not provided. Cannot create folder.")
            return None

        # Get the user data to find the root Node URI
        username, user_data = self.get_user_endpoint()
        if not username or not user_data:
            logging.error("Could not get user data. Cannot create folder.")
            return None

        # Try to find the root Node URI in user data
        uris = user_data.get('Response', {}).get('User', {}).get('Uris', {})
        node_uri = uris.get('Node', {}).get('Uri')  # This is the root node URI

        if not node_uri:
            logging.error("Could not find root Node URI in SmugMug API response. Cannot create folder.")
            # Log available URIs for debugging if Node URI is not found
            available_uris = list(uris.keys()) if uris else []
            logging.error(f"Available URIs in user data: {available_uris}")
            return None

        # The correct URI for creating a folder *under* a node is the node's own URI
        # You POST the new folder data to the parent node's URI.
        folder_creation_uri = node_uri
        logging.info(f"Using Node URI ({folder_creation_uri}) for folder creation.")

        try:
            # Prepare the folder creation request
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }

            # Set folder settings with desired defaults
            # Include 'Type': 'Folder' and 'Name'
            folder_data = {
                'Name': folder_name,
                'UrlName': folder_name.title().replace(' ', ''),  # First character needs to be uppercase
                'Privacy': 'Private',  # Make new folders private by default
                'Type': 'Folder'  # Explicitly set type to Folder
            }

            # Create the folder by POSTing to the parent node's URI
            url = f"https://api.smugmug.com{folder_creation_uri}"
            logging.info(f"Creating folder '{folder_name}' by POSTing to URL: {url}")
            response = self.auth_session.post(url, headers=headers, json=folder_data)
            response.raise_for_status()
            data = response.json()

            # Extract the folder URI from the response
            folder = data.get('Response', {}).get('Folder', {})
            new_folder_uri = folder.get('Uri')

            if not new_folder_uri:
                logging.error(f"Folder '{folder_name}' created, but missing Uri in response.")
                logging.error(f"Response: {data}")
                return None

            logging.info(f"Created folder '{folder_name}' with URI {new_folder_uri}")
            return new_folder_uri

        except requests.exceptions.RequestException as e:
            logging.error(f"Error creating folder '{folder_name}' on SmugMug: {e}")
            if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                logging.error(f"HTTP Status Code: {e.response.status_code}")
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                logging.error(f"SmugMug API Response Text: {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"An unexpected error occurred while creating folder '{folder_name}' on SmugMug: {e}")
            return None

    def get_or_create_folder(self, folder_name):
        """
        Gets an existing folder by name or creates a new one if it doesn't exist.
        Returns the folder URI if successful, None otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot get or create folder.")
            return None

        if not folder_name:
            logging.error("Folder name not provided. Cannot get or create folder.")
            return None

        logging.info(f"Attempting to find or create folder: '{folder_name}'")

        # First, try to select the folder by name
        folder_uri = self.select_folder_by_name(folder_name)
        if folder_uri:
            return folder_uri

        # If the folder doesn't exist, create it
        logging.info(f"Folder '{folder_name}' not found. Creating a new folder.")
        return self.create_folder(folder_name)

    def select_folder_by_name(self, folder_name):
        """
        Selects a folder by name and returns its URI.
        Returns the folder URI if successful, None otherwise.
        """
        if not self.auth_session:
            logging.error("Not authenticated with SmugMug. Cannot select folder.")
            return None

        if not folder_name:
            logging.error("Folder name not provided. Cannot select folder.")
            return None

        # Get all folders
        folders = self.list_folders()
        if not folders:
            logging.error("No folders found. Cannot select folder.")
            return None

        # Find the folder with the matching name (case-insensitive)
        for folder in folders:
            # Check if folder is a dictionary before using get method
            if isinstance(folder, dict):
                if folder.get('Name', '').lower() == folder_name.lower():
                    folder_uri = folder.get('Uri')

                    if not folder_uri:
                        logging.error(f"Folder '{folder_name}' found, but missing Uri.")
                        return None

                    logging.info(f"Selected folder '{folder_name}' with URI {folder_uri}")
                    return folder_uri
            elif isinstance(folder, str):
                # If folder is a string, it might be a URI or a name
                # Log this for debugging
                logging.warning(f"Found folder as string: {folder}")
                # If the folder string is the folder name (case-insensitive)
                if folder.lower() == folder_name.lower():
                    logging.info(f"Selected folder '{folder_name}' with URI (from string)")
                    return folder

        logging.info(f"Folder '{folder_name}' not found in SmugMug account.")
        return None

    @staticmethod
    def calculate_file_hash(file_path, hash_algorithm='md5'):
        """Calculates the hash of a file using the specified algorithm (default: md5)."""
        if hash_algorithm.lower() == 'md5':
            hasher = hashlib.md5()
        elif hash_algorithm.lower() == 'sha256':
            hasher = hashlib.sha256()
        else:
            logging.error(f"Unsupported hash algorithm: {hash_algorithm}")
            return None

        try:
            with open(file_path, 'rb') as afile:
                # Read in chunks to handle large files
                while True:
                    chunk = afile.read(65536)  # 64KB chunk size
                    if not chunk:
                        break
                    hasher.update(chunk)
                return hasher.hexdigest()
        except FileNotFoundError:
            logging.error(f"File not found: {file_path}")
            return None
        except Exception as e:
            logging.error(f"Error calculating {hash_algorithm.upper()} hash for {file_path}: {e}")
            return None
