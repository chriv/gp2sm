# gp2sm
# Google Photos to SmugMug Transfer Tool (v1.6)

A Python tool for transferring media (photos and videos) from Google Photos to SmugMug, featuring duplicate checking, HEIC handling options, automatic configuration file generation, enhanced authentication handling, colorized logging, and graceful shutdown.

## Important Notes

* This project is not affiliated with, endorsed by, or officially supported by Google™ or SmugMug®.
* Google Photos™ is a trademark of Google LLC.
* SmugMug® is a registered trademark of SmugMug, Inc.

## Features

* Transfer photos and videos from Google Photos to SmugMug.
* Duplicate Checking:
    * Checks for existing **images** on SmugMug using MD5 hash comparison.
    * Checks for existing **videos** on SmugMug using filename comparison.
* HEIC File Handling: Ignores Apple HEIC files by default, with an option to process them (converting them to JPGs on SmugMug, disabling duplicate checks for them).
* Google Photos Album Support: Optionally process only media items from a specific Google Photos album ID.
* SmugMug Folder/Album Management:
    * Specify target album by name or by key/URI.
    * Optionally specify a target folder path (e.g., "Folder/SubFolder").
    * Automatically creates folders and/or albums if they don't exist.
* Configuration Assistance:
    * Generates a default `smugmug_config.json` if it's missing.
    * Adds missing required fields with placeholders to an existing `smugmug_config.json` if incomplete.
    * Provides clear instructions if the Google API credentials file (`google_api_keys.json`) is missing.
* Logging:
    * Colorized console output for different message levels (INFO, SUCCESS, WARNING, ERROR, DEBUG).
    * Enhanced console logging readability with more distinct message colors (v1.6).
    * Detailed file logging to `gp2sm_transfer.log` (with log rotation).
* Enhanced Authentication Handling: Includes automatic refresh for expired Google Photos tokens and proactive refresh before expiry. Re-fetches Google Photos item details if download URLs expire during processing.
* Graceful Shutdown (v1.6): Handles Ctrl+C and termination signals to stop cleanly after the current item and perform resource cleanup (lock file, temporary directory).
* Dry Run Mode: Simulate the transfer process without uploading or modifying files.
* Selective Transfer: Options to ignore photos or ignore videos during transfer.
* Lock File: Prevents multiple instances of the script from running simultaneously.
* Token Storage: Securely stores and reuses OAuth tokens locally (`google_photos_token.json`, `smugmug_config.json`) to minimize re-authentication.

## Prerequisites

* Python 3.7 or higher.
* `pip` (Python package installer).

## Setup

1.  **Clone or Download:** Get the script files (`main.py`, `google_photos_module.py`, `smugmug_module.py`, `requirements.txt`, etc.).
2.  **Install Dependencies:** Open a terminal or command prompt in the script's directory and run:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Configure Google Photos API:**
    * Follow the instructions in the Google Cloud Console to create an **OAuth 2.0 Client ID for Desktop application**. See: [Google Cloud Console](https://console.cloud.google.com/).
    * Enable the **Google Photos Library API** for your project.
    * Download the client secrets JSON file.
    * **IMPORTANT:** Rename the downloaded file to exactly `google_api_keys.json` and place it in the same directory as the script.
    * If you run the script and this file is missing, it will print detailed instructions to the console and log.
4.  **Configure SmugMug API:**
    * Register an application on the SmugMug Developer Portal to get your API Key and Secret. See: [SmugMug Developer Apply](https://api.smugmug.com/api/developer/apply).
    * **Option 1 (Recommended):** Run the script once (`python main.py`). If `smugmug_config.json` doesn't exist, the script will create a default one for you and exit.
    * **Option 2 (Manual):** Copy `smugmug_config.json.example.txt` to `smugmug_config.json`.
    * Edit `smugmug_config.json`:
        * Fill in your obtained `api_key` and `api_secret`.
        * Choose **ONE** method to specify the target album:
            * Set `album_name` to your desired album name (e.g., "Google Photos Import"). Leave `album_key` and `album_api_uri` as placeholders.
            * **OR**, find the **Album Key** for an *existing* SmugMug album (usually in the album's URL) and update `album_key` and `album_api_uri`. Set `album_name` to its placeholder value (`"YOUR_SMUGMUG_ALBUM_NAME"`).
        * Optionally, set `folder_name` to a folder path (e.g., `"My Photos/Google Imports"`). Folders will be created if they don't exist.
        * Leave `oauth_token` and `oauth_token_secret` as placeholders; the script will handle authentication and save them automatically.
        * Optionally, set `process_heic` to `true` if you want to process HEIC files (see warnings below).

## Running the Script

1.  **Navigate:** Open your terminal or command prompt to the directory containing `main.py`.
2.  **Run:** Execute the script using:
    ```bash
    python main.py [OPTIONS]
    ```
    *(See Command-Line Arguments below for available `[OPTIONS]`)*
3.  **First-Time Authorization:**
    * **Google Photos:** The script will open a web browser asking you to log in to your Google account and grant permission for the script to access your photos. Follow the prompts. After granting permission, tokens will be saved to `google_photos_token.json`.
    * **SmugMug:** The script will print a URL. Open this URL in your browser, log in to SmugMug, and authorize the application. SmugMug will provide a verifier code (usually 6 digits). Copy this code and paste it back into the terminal when prompted. The script will then save the access tokens into `smugmug_config.json`.
4.  **Configuration Validation:** The script checks `smugmug_config.json` for required fields. If essential fields (like API keys or album specification) are missing or still have placeholder values, the script will update the file with the necessary placeholders/comments and exit, prompting you to edit the file.
5.  **Subsequent Runs:** The script will use the saved tokens and configuration, running the transfer process. It will attempt to automatically refresh tokens if they expire or are close to expiring.
6.  **Interrupting:** You can press `Ctrl+C` to request a graceful shutdown. The script will attempt to finish processing the current item before cleaning up and exiting.
7.  **Logging:** Check the console for colorized status messages and review the `gp2sm_transfer.log` file for detailed logs.
8.  **Lock File:** A `gp2sm.lock` file is created while the script runs to prevent multiple instances. If the script crashes or is force-quit, you may need to delete this file manually before running again.

### Command-Line Arguments

* `--google-photos-album-id <ALBUM_ID>`: (Optional) Process only media items from the specified Google Photos album ID. If omitted, processes the entire library.
* `--delete-from-google`: (Optional) Simulate deleting items from Google Photos after successful upload/check. **Warning:** The Google Photos API *does not currently support deletion*, so this flag only logs what *would* be deleted. No actual deletion occurs.
* `--smugmug-album <ALBUM_NAME>`: (Optional) Specify the name of the target SmugMug album. Overrides the `album_name`, `album_key`, and `album_api_uri` settings in the config file. If the album doesn't exist in the target location, it will be created.
* `--smugmug-folder <FOLDER_PATH>`: (Optional) Specify the SmugMug folder path (e.g., `"Vacations/Europe 2024"`) to place the album in. Overrides the `folder_name` setting in the config file. Folders in the path will be created if they don't exist.
* `--dry-run`: (Optional) Perform a dry run: check existence, log actions, but do not upload to SmugMug or simulate deletion. Downloads may still occur for MD5 checking.
* `--ignore-photos`: (Optional) Skip processing media items identified as photos (images).
* `--ignore-videos`: (Optional) Skip processing media items identified as videos.
* `--process-heic`: (Optional) Process HEIC files (Apple Live Photos). Default is to ignore them. See "HEIC File Handling" below for important implications.
* `--debug`: (Optional) Enable debug logging, providing more verbose output to both the console and the log file.
* `--version`: Show the script's version number and exit.
* `-h`, `--help`: Show the help message and exit.

## How Duplicate Checking Works

* **Images:** The script downloads the image from Google Photos, calculates its MD5 hash, and then checks if any image in the target SmugMug album has a matching `ArchivedMD5` value. This prevents uploading exact duplicates.
* **Videos:** Because SmugMug re-encodes videos upon upload, their MD5 hash changes. Therefore, the script checks for existing videos by comparing the `FileName` from Google Photos (case-insensitive) against the filenames of items already in the target SmugMug album. This check happens *before* downloading the video to save bandwidth if a filename match is found.
* **HEIC Files:** If `--process-heic` is enabled, **no duplicate checking** is performed for these files due to SmugMug's conversion process.

## HEIC File Handling (Apple Live Photos)

* Apple Live Photos typically consist of a `.jpg` image and a `.mov` or `.heic` file containing the short video portion.
* Google Photos preserves both parts.
* SmugMug accepts `.heic` uploads but **converts them into static `.jpg` images**, changing the file extension and content. The 'live' video aspect is lost.
* Because of this conversion, it's impossible to reliably check for duplicates of `.heic` files on SmugMug based on filename or hash after they have been uploaded and converted.
* **By default, this script IGNORES `.heic` files**.
* You can enable processing of `.heic` files using the `--process-heic` command-line flag or by setting `"process_heic": true` in `smugmug_config.json`.
* **If you enable HEIC processing, be aware that:**
    * The live video portion will be lost on SmugMug.
    * Duplicate checking for `.heic` files is completely skipped. You **may end up with duplicate *converted* JPGs** on SmugMug if you run the script multiple times with this option enabled.

## License

This project is released into the public domain under The Unlicense. See the [LICENSE](LICENSE) file for details.