# gp2sm

This Python script (version 1.0) transfers photos and videos from your Google Photos™ library to a specified SmugMug® album. It includes features to handle authentication, check for duplicates, and optionally delete items from Google Photos™ after confirming their existence on SmugMug®.

## Important Notes

*   This project is not affiliated with, endorsed by, or officially supported by Google™ or SmugMug®.
*   Google Photos™ is a trademark of Google LLC.
*   SmugMug® is a registered trademark of SmugMug, Inc.

## License

This project is released into the public domain under The Unlicense. See the [LICENSE](LICENSE) file for details.

## Features

*   **Google Photos Integration:** Authenticates with Google Photos using OAuth 2.0 and fetches media items.
*   **SmugMug Integration:** Authenticates with SmugMug using OAuth 1.0a and uploads media to a specified album.
*   **Handles Photos and Videos:** Correctly downloads and uploads both image and video file types.
*   **Duplicate Checking:**
    *   Checks for existing **images** on SmugMug using MD5 hash comparison to prevent exact duplicates.
    *   Checks for existing **videos** on SmugMug using filename comparison (as SmugMug re-encodes videos, making hash comparison unreliable).
*   **Specific Album Support:** Can optionally process only photos from a specific Google Photos album using its ID.
*   **Optional Deletion:** Provides an option (`--delete-from-google`) to prompt for deletion of media from Google Photos *if* it's confirmed to already exist on SmugMug.
*   **Configuration Files:** Uses simple JSON files for API credentials and settings.
*   **Token Storage:** Securely stores and reuses OAuth tokens locally (`google_photos_token.json`, `smugmug_config.json`) to avoid repeated logins.

## Prerequisites

*   Python 3.6 or higher
*   `pip` (Python package installer)

## Setup

1.  **Clone or Download:** Get the script files (`main.py`, `requirements.txt`, etc.).
2.  **Install Dependencies:** Open a terminal or command prompt in the script's directory and run:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Configure Google Photos API:**
    *   Follow the instructions in the Google Cloud Console to create an **OAuth 2.0 Client ID for Desktop application**. See: [Google Cloud Console](https://console.cloud.google.com/)
    *   Enable the **Google Photos Library API** for your project.
    *   Download the client secrets JSON file.
    *   Rename the downloaded file to `google_photos_credentials.json` and place it in the same directory as the script.
    *   Copy `google_photos_credentials.json.example` to `google_photos_credentials.json` and fill in the `client_id` and `client_secret` from the downloaded file.
4.  **Configure SmugMug API:**
    *   Register an application on the SmugMug Developer Portal to get your API Key and Secret. See: [SmugMug Developer Apply](https://api.smugmug.com/api/developer/apply)
    *   Copy `smugmug_config.json.example` to `smugmug_config.json`.
    *   Fill in your `api_key` and `api_secret` in `smugmug_config.json`.
    *   Find the **Album Key** for your target SmugMug album (this is usually part of the album's URL) and update `album_key` and `album_api_uri` accordingly in `smugmug_config.json`.
    *   Leave `oauth_token` and `oauth_token_secret` as placeholders initially; the script will populate these after the first successful authorization.

## Running the Script

1.  **Navigate:** Open your terminal or command prompt to the directory containing `main.py`.
2.  **Run:** Execute the script using:
    ```bash
    python main.py
    ```
3.  **First-Time Authorization:**
    *   **Google Photos:** The script will likely open a web browser asking you to log in to your Google account and grant permission for the script to access your photos. After granting permission, the necessary tokens will be saved to `google_photos_token.json`.
    *   **SmugMug:** The script will print a URL. Open this URL in your browser, log in to SmugMug, and authorize the application. SmugMug will provide a verifier code. Copy this code and paste it back into the terminal when prompted. The script will then save the access tokens to `smugmug_config.json`.
4.  **Subsequent Runs:** The script will use the saved tokens and should run without requiring browser interaction unless the tokens expire or are revoked.

### Command-Line Arguments

*   `--google-photos-album-id <ALBUM_ID>`: (Optional) Process only media items from the specified Google Photos album ID. If omitted, processes the entire library.
    ```bash
    python main.py --google-photos-album-id YOUR_GOOGLE_ALBUM_ID_HERE
    ```
*   `--delete-from-google`: (Optional) If a media item is found to already exist on SmugMug (based on hash for images, filename for videos), prompt the user for confirmation before attempting to delete it from Google Photos. **Use with caution!**
    ```bash
    python main.py --delete-from-google
    ```

## How Duplicate Checking Works

*   **Images:** The script downloads the image from Google Photos, calculates its MD5 hash, and then checks if any image in the target SmugMug album has a matching `ArchivedMD5` value.
*   **Videos:** Because SmugMug re-encodes videos upon upload, their MD5 hash changes. Therefore, the script checks for existing videos by comparing the `FileName` from Google Photos against the filenames of items already in the target SmugMug album. This check happens *before* downloading the video to save bandwidth if it already exists.

## License

This project is released into the public domain under The Unlicense. See the [LICENSE](LICENSE) file for details.