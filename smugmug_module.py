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
import os
import json
import logging
import hashlib

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
        return self.auth_session is not None and self.album_key is not None and self.album_api_uri is not None
    
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
            
            # Set album details
            self.album_key = self.config.get('album_key')
            self.album_api_uri = self.config.get('album_api_uri')
            
            if not self.album_key or not self.album_api_uri:
                logging.error("SmugMug 'album_key' and 'album_api_uri' must be set in the configuration file.")
                return False
                
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
            return False # Cannot check image without hash

        # Construct the API URL to list images in the album
        album_details_url = f"https://api.smugmug.com/api/v2/album/{album_key}"
        headers = {'Accept': 'application/json'}

        try:
            # First, get album details to find the Images URI
            album_response = self.auth_session.get(album_details_url, headers=headers)
            album_response.raise_for_status() # Raise for HTTP errors
            album_data = album_response.json()

            # Navigate through the response to find the Images URI
            images_uri = album_data.get('Response', {}).get('Album', {}).get('Uris', {}).get('AlbumImages', {}).get('Uri')

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
                        item_type = item.get('Type') # SmugMug might distinguish 'Image' and 'Video' types

                        # Check based on media type
                        if is_video:
                            # For videos, compare filenames
                            if item_filename and filename.lower() == item_filename.lower() and (item_type is None or item_type == 'Video'):
                                return True
                        else:
                            # For images, compare MD5 hashes
                            smugmug_md5 = item.get('ArchivedMD5') # This field stores the MD5 hash
                            if file_hash and smugmug_md5 and file_hash.lower() == smugmug_md5.lower() and (item_type is None or item_type == 'Image'):
                                logging.info(f"Image '{filename}' found on SmugMug with matching MD5 hash: {file_hash}")
                                return True # Exact match found

                # Pagination logic
                pages_info = data.get('Response', {}).get('Pages')
                if pages_info and 'NextPage' in pages_info:
                    # Construct the full URL for the next page
                    next_page_url = f"https://api.smugmug.com{pages_info['NextPage']}"
                else:
                    next_page_url = None # No more pages

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
                logging.info(f"Successfully initiated upload for '{filename}'. SmugMug Status URL: {status_url}, Final URL (approx): {image_url}")
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
                    chunk = afile.read(65536) # 64KB chunk size
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