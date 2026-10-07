"""SmugMug OAuth 1.0a out-of-band (PIN) sign-in."""

from requests_oauthlib import OAuth1Session

REQUEST_TOKEN_URL = "https://secure.smugmug.com/services/oauth/1.0a/getRequestToken"
AUTHORIZE_URL = "https://secure.smugmug.com/services/oauth/1.0a/authorize?Access=Full&Permissions=Modify"
ACCESS_TOKEN_URL = "https://secure.smugmug.com/services/oauth/1.0a/getAccessToken"
REQUIRED = ("api_key", "api_secret", "oauth_token", "oauth_token_secret")


def pin_flow(api_key, api_secret, ask_pin, show, session_factory=OAuth1Session):
    """Run the PIN flow. ask_pin(prompt) -> str; show(text) displays the authorize URL. Returns credentials."""
    oauth = session_factory(api_key, client_secret=api_secret, callback_uri="oob")
    oauth.fetch_request_token(REQUEST_TOKEN_URL)
    show(f"Open this URL, sign in to SmugMug, and approve access:\n\n  {oauth.authorization_url(AUTHORIZE_URL)}\n")
    pin = ask_pin("6-digit code from SmugMug: ").strip()
    tokens = oauth.fetch_access_token(ACCESS_TOKEN_URL, verifier=pin)
    return {"api_key": api_key, "api_secret": api_secret,
            "oauth_token": tokens["oauth_token"], "oauth_token_secret": tokens["oauth_token_secret"]}


def check(creds):
    missing = [k for k in REQUIRED if not creds.get(k) or str(creds[k]).startswith("YOUR_")]
    if missing:
        raise ValueError(f"credentials are missing {missing}")
    return {k: creds[k] for k in REQUIRED}
