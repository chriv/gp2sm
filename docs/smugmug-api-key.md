# Getting a SmugMug API key

gp2sm talks to SmugMug through its API, which needs a key of your own. SmugMug issues one for free to any account; you only need to do this once.

1. Sign in to SmugMug, open the [SmugMug API documentation](https://api.smugmug.com/api/v2/doc), and choose **Request a new API key** (or **Manage Applications** in the upper right). SmugMug's own instructions: [SmugMug API help](https://www.smugmughelp.com/hc/en-us/articles/18212102704916-SmugMug-API).
2. Fill in the form. Any name works, e.g. "gp2sm (personal)". gp2sm doesn't use a callback URL, so leave it empty if SmugMug allows that, or enter your own site's address.
3. Apply. SmugMug shows the new key and its **secret**. Keep the secret private: anyone with both can act as your application.
4. Sign gp2sm in:
   ```bash
   gp2sm auth smugmug
   ```
   Enter the key and the secret (the secret isn't shown as you type). gp2sm prints a SmugMug web address. Open it, approve access, and type the 6-digit code SmugMug shows you. gp2sm checks the result by asking SmugMug who you are.

The credentials are stored for your user account, outside any project folder (on macOS in `~/Library/Application Support/gp2sm/credentials/`, on Linux in `~/.config/gp2sm/credentials/`, on Windows in `%APPDATA%\gp2sm\credentials\`), and readable only by you. `gp2sm auth list` shows what's stored, and `gp2sm auth remove NAME --yes` deletes it. To use several SmugMug accounts, sign in with `--name` and set `credentials = "that-name"` in each project's `gp2sm.toml`.
