"""SmugMug API v2 client.

Every behavior relied on here is recorded in docs/smugmug-api.md. Notable traps this client handles:
- list endpoints use different response keys (e.g. !images -> "AlbumImage", not "Image")
- upload/API failures can arrive as HTTP 200 with stat="fail"
- 401 "nonce_used" after slow requests is retryable
- batch moves are all-or-nothing (400 moves nothing)
- a 504/timeout on a write may still have been applied server-side, so writes are never blindly
  retried after one (a retried move then fails with 400 because the images already moved)
"""

import json
import logging
import re
import threading
import time

import requests
from requests_oauthlib import OAuth1Session

from gp2sm.services.base import Capabilities

API = "https://api.smugmug.com"
UPLOAD_URL = "https://upload.smugmug.com/"
log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Behavior verified by probes (docs/smugmug-api.md), declared for the service-neutral core.
SIZES = {"medium": "Medium", "large": "Large", "xlarge": "XLarge", "x2large": "X2Large", "x3large": "X3Large",
         "x4large": "X4Large", "x5large": "X5Large", "4k": "4K", "5k": "5K", "original": "Original"}
# Neutral album setting -> (SmugMug Album field, {neutral value: SmugMug value}, or None for a boolean).
# Each was confirmed to round-trip by the A5 probe (docs/smugmug-api.md "Album and folder settings").
ALBUM_SETTINGS = {
    "privacy": ("Privacy", {"public": "Public", "unlisted": "Unlisted", "private": "Private"}),
    "search": ("SmugSearchable", {"inherit": "Inherit from User", "no": "No"}),
    "web_search": ("WorldSearchable", None),
    "downloads": ("AllowDownloads", None),
    "download_size": ("MaxPhotoDownloadSize", SIZES),
    "largest_size": ("LargestSize", SIZES),
    "protected": ("Protected", None),
    "watermark": ("Watermark", None),
    "share": ("Share", None),
    "comments": ("Comments", None),
    "ranking": ("CanRank", None),
    "exif": ("EXIF", None),
    "filenames": ("Filenames", None),
    "geography": ("Geography", None),
    "slideshow": ("Slideshow", None),
    "printable": ("Printable", None),
    "hide_owner": ("HideOwner", None),
    "sort": ("SortMethod", {"position": "Position", "caption": "Caption", "filename": "Filename",
                            "date_uploaded": "Date Uploaded", "date_modified": "Date Modified",
                            "date_taken": "Date Taken"}),
    "sort_direction": ("SortDirection", {"ascending": "Ascending", "descending": "Descending"}),
}

SMUGMUG_CAPABILITIES = Capabilities(
    name="smugmug",
    atomic_batch_moves=True,
    writes_may_apply_despite_error=True,
    moves_ignored_while_processing=True,
    can_rename_items=False,
    allows_duplicate_uploads=True,
    stores_original_bytes=frozenset({".jpg", ".jpeg", ".png", ".gif"}),
    converts_on_upload=((".heic", ".jpg"), (".heif", ".jpg")),
    rejected_extensions=frozenset({".webp", ".ico", ".bmp"}),
    min_video_pixels=None,  # low-resolution clips are rejected; exact threshold unknown (~250-460 px long side)
    max_items_per_album=5000,
    can_collect=True,
    removing_original_removes_collected=True,
    album_settings={k: tuple(values) if values else (True, False) for k, (_, values) in ALBUM_SETTINGS.items()},
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SmugMugError(Exception):
    """An API call failed in a way that retrying will not fix."""

    def __init__(self, message, http_status=None, code=None, body=None, ambiguous=False):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.body = body
        self.ambiguous = ambiguous  # a write whose outcome is unknown (timeout/5xx/network)


class NotFound(SmugMugError):
    """HTTP 404."""


def url_name_for(name):
    """SmugMug UrlName: alphanumerics and dashes, starting with an uppercase letter."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    if not slug:
        slug = "Album"
    if not slug[0].isalpha():
        slug = "A-" + slug
    return slug[0].upper() + slug[1:]


class SmugMugClient:
    capabilities = SMUGMUG_CAPABILITIES
    def __init__(self, api_key, api_secret, token, token_secret, *, max_retries=6, page_size=100,
                 session_factory=None, sleep=time.sleep, min_ratelimit_remaining=200):
        self._creds = (api_key, api_secret, token, token_secret)
        self._local = threading.local()
        self._session_factory = session_factory or self._default_session
        self.max_retries = max_retries
        self.page_size = page_size
        self._sleep = sleep
        self.min_ratelimit_remaining = min_ratelimit_remaining
        self.ratelimit_remaining = None

    @classmethod
    def from_config_file(cls, path, **kw):
        """Build from a smugmug_config.json (api_key, api_secret, oauth_token, oauth_token_secret)."""
        with open(path) as f:
            cfg = json.load(f)
        return cls(cfg["api_key"], cfg["api_secret"], cfg["oauth_token"], cfg["oauth_token_secret"], **kw)

    def _default_session(self):
        api_key, api_secret, token, token_secret = self._creds
        return OAuth1Session(api_key, client_secret=api_secret, resource_owner_key=token,
                             resource_owner_secret=token_secret)

    @property
    def session(self):
        """One OAuth1 session per thread."""
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        return self._local.session

    # ------------------------------------------------------------------ core

    def request(self, method, path, *, params=None, json_body=None, data=None, headers=None, timeout=90,
                idempotent=False):
        """Perform an API call and return the parsed JSON body.

        Retries 429 and 401 nonce_used (the request was rejected, so retrying is safe). Network errors
        and 5xx are retried only for read methods and writes marked idempotent=True; for writes they raise SmugMugError(ambiguous=True)
        because the server may already have applied the change. Raises NotFound on 404 and
        SmugMugError on any other failure (including HTTP 200 with stat="fail").
        """
        url = path if path.startswith("http") else API + path
        safe = method.upper() in SAFE_METHODS or idempotent  # idempotent: setting absolute values
        last_error = None
        for attempt in range(self.max_retries):
            if attempt:
                self._sleep(min(60, 3 * 2 ** (attempt - 1)))
            try:
                r = self.session.request(method, url, params=params, json=json_body, data=data, timeout=timeout,
                                         headers={"Accept": "application/json", **(headers or {})})
            except requests.RequestException as e:
                last_error = SmugMugError(f"{method} {url}: network error {e!r}", ambiguous=not safe)
                log.warning("%s (attempt %d)", last_error, attempt + 1)
                if not safe:
                    raise last_error from e
                continue
            self._note_ratelimit(r)
            try:
                body = r.json()
            except ValueError:
                body = {"_text": r.text[:500]}
            log.debug("%s %s -> %s %s", method, url, r.status_code, json.dumps(body, default=str)[:2000])

            rejected = r.status_code == 429 or (r.status_code == 401 and "nonce_used" in r.text)
            if rejected or r.status_code in RETRY_STATUSES:
                last_error = SmugMugError(f"{method} {url}: HTTP {r.status_code} {self._message(body)}",
                                          http_status=r.status_code, body=body, ambiguous=not (safe or rejected))
                log.warning("%s (attempt %d)", last_error, attempt + 1)
                if last_error.ambiguous:
                    raise last_error
                continue
            if r.status_code == 404:
                raise NotFound(f"{method} {url}: not found", http_status=404, body=body)
            if r.status_code >= 400:
                raise SmugMugError(f"{method} {url}: HTTP {r.status_code} {self._message(body)}",
                                   http_status=r.status_code, code=body.get("Code"), body=body)
            if isinstance(body, dict) and body.get("stat") == "fail":
                raise SmugMugError(f"{method} {url}: stat=fail code={body.get('code')} {body.get('message')}",
                                   http_status=r.status_code, code=body.get("code"), body=body)
            return body
        raise last_error

    @staticmethod
    def _message(body):
        if not isinstance(body, dict):
            return ""
        return body.get("Message") or body.get("message") or body.get("_text", "")

    def _note_ratelimit(self, response):
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is None:
            return
        try:
            self.ratelimit_remaining = int(remaining)
        except ValueError:
            return
        if self.ratelimit_remaining < self.min_ratelimit_remaining:
            reset = response.headers.get("x-ratelimit-reset")
            wait = 60
            if reset and reset.isdigit():
                wait = max(1, int(reset) - int(time.time()))
            log.warning("SmugMug rate limit low (%s left); sleeping %ss", remaining, wait)
            self._sleep(min(wait, 3600))

    def paged(self, path, list_key, params=None):
        """Yield (item, expansions) for every item of a paged list endpoint."""
        params = dict(params or {})
        params.setdefault("count", self.page_size)
        url = path
        while url:
            body = self.request("GET", url, params=params)
            params = None  # NextPage already carries the query string
            resp = body.get("Response", {})
            expansions = body.get("Expansions", {})
            for item in resp.get(list_key, []) or []:
                yield item, expansions
            url = resp.get("Pages", {}).get("NextPage")

    # ------------------------------------------------------------- read API

    def authuser(self):
        return self.request("GET", "/api/v2!authuser")["Response"]["User"]

    def user_albums(self, nickname):
        for album, _ in self.paged(f"/api/v2/user/{nickname}!albums", "Album"):
            yield album

    def album(self, album_key):
        return self.request("GET", f"/api/v2/album/{album_key}")["Response"]["Album"]

    def album_images(self, album_key, with_metadata=True, fields=None):
        """Yield (AlbumImage dict, ImageMetadata dict or {}) for every image in an album.

        fields: optional comma-separated AlbumImage field names (via _filter) for much smaller, faster pages;
        ignored when with_metadata is True (the expansion needs the Uris block).
        """
        if with_metadata:
            params = {"_expand": "ImageMetadata"}
        elif fields:
            params = {"_filter": fields, "_filteruri": ""}
        else:
            params = {}
        for img, exp in self.paged(f"/api/v2/album/{album_key}!images", "AlbumImage", params):
            md_uri = img.get("Uris", {}).get("ImageMetadata", {}).get("Uri")
            md = exp.get(md_uri, {}).get("ImageMetadata", {}) if md_uri else {}
            yield img, md

    def node_children(self, node_uri):
        for node, _ in self.paged(f"{node_uri}!children", "Node"):
            yield node

    def album_has_image(self, album_key, image_key, serial=0):
        try:
            self.request("GET", f"/api/v2/album/{album_key}/image/{image_key}-{serial}")
            return True
        except NotFound:
            return False

    def image_album_keys(self, image_key, serial=0):
        body = self.request("GET", f"/api/v2/image/{image_key}-{serial}!albums")
        return [a.get("AlbumKey") for a in body.get("Response", {}).get("Album", []) or []]

    # ------------------------------------------------------------ write API

    def create_node(self, parent_node_uri, node_type, name, privacy="Private"):
        body = self.request("POST", f"{parent_node_uri}!children",
                            json_body={"Type": node_type, "Name": name, "UrlName": url_name_for(name),
                                       "Privacy": privacy})
        return body["Response"]["Node"]

    def find_child(self, parent_node_uri, node_type, name):
        for node in self.node_children(parent_node_uri):
            if node.get("Type") == node_type and node.get("Name") == name:
                return node
        return None

    def ensure_folder_path(self, root_node_uri, path):
        """Find or create each folder in 'A/B/C' under root; return the last folder's node URI."""
        node_uri = root_node_uri
        for part in [p for p in path.split("/") if p.strip()]:
            node = self.find_child(node_uri, "Folder", part)
            if node is None:
                node = self.create_node(node_uri, "Folder", part)
                log.info("created folder %r -> %s", part, node["Uri"])
            node_uri = node["Uri"]
        return node_uri

    def ensure_album(self, parent_node_uri, name):
        """Find or create an album by exact Name under a folder node.

        Returns (album_key, album_uri, node_uri, created: bool).
        """
        node = self.find_child(parent_node_uri, "Album", name)
        created = False
        if node is None:
            node = self.create_node(parent_node_uri, "Album", name)
            created = True
        album_uri = node["Uris"]["Album"]["Uri"]
        return album_uri.rsplit("/", 1)[-1], album_uri, node["Uri"], created

    def move_images(self, dest_album_key, album_image_uris):
        """Move AlbumImages into dest album. All-or-nothing: raises SmugMugError (usually 400) if any URI is bad."""
        self.request("POST", f"/api/v2/album/{dest_album_key}!moveimages",
                     json_body={"MoveUris": ",".join(album_image_uris)})

    def upload(self, album_uri, path, filename, content_type, timeout=600):
        """Upload a file into an album. Returns the response's Image dict (ImageUri, AlbumImageUri, ...).

        Not idempotent: on timeout/5xx/network errors this raises SmugMugError(ambiguous=True); the caller
        must check the album before retrying. Failures such as unsupported types arrive as HTTP 200 with
        stat="fail" and raise SmugMugError(code=...).
        """
        import hashlib
        with open(path, "rb") as f:
            data = f.read()
        headers = {
            "X-Smug-AlbumUri": album_uri,
            "X-Smug-FileName": filename,
            "X-Smug-ResponseType": "JSON",
            "X-Smug-Version": "v2",
            "Content-MD5": hashlib.md5(data).hexdigest(),
            "Content-Type": content_type,
        }
        body = self.request("POST", UPLOAD_URL, data=data, headers=headers, timeout=timeout)
        image = body.get("Image")
        if not image:
            raise SmugMugError(f"upload {filename}: no Image in response", body=body, ambiguous=True)
        return image

    def set_album_sort(self, album_key, method="FileName", direction="Ascending"):
        return self.request("PATCH", f"/api/v2/album/{album_key}", idempotent=True,
                            json_body={"SortMethod": method, "SortDirection": direction})["Response"]["Album"]

    # ------------------------------------------------------------------
    # Neutral surface. The rest of gp2sm uses only these methods and the plain dicts they return, never
    # SmugMug URLs or field names. (They'll become the SmugMug adapter's PhotoDestination implementation
    # in ROADMAP A1.)
    #
    # item dict:  item_id, serial, item_ref, name, format, is_video, md5, size, width, height,
    #             duration_s, uploaded, capture_time, raw, raw_metadata
    # album dict: album_id, ref, name, path, item_count, raw
    # ------------------------------------------------------------------

    @staticmethod
    def _duration_s(value):
        import re as _re
        m = _re.search(r"[\d.]+", str(value or ""))
        return float(m.group()) if m else None

    @classmethod
    def _neutral_item(cls, img, md=None):
        md = md or {}
        return {
            "item_id": img.get("ImageKey"), "serial": img.get("Serial") or 0, "item_ref": img.get("Uri"),
            "name": img.get("FileName"), "format": img.get("Format"), "is_video": bool(img.get("IsVideo")),
            "md5": img.get("ArchivedMD5"), "size": img.get("ArchivedSize"),
            "width": img.get("OriginalWidth"), "height": img.get("OriginalHeight"),
            "duration_s": cls._duration_s(md.get("Duration")), "uploaded": img.get("DateTimeUploaded"),
            "capture_time": md.get("DateTimeCreated") or None,
            "make": md.get("Make") or None, "model": md.get("Model") or None,
            "raw": {k: v for k, v in img.items() if k != "Uris"}, "raw_metadata": md or None,
        }

    def root_folder(self):
        """Reference of the account's root folder."""
        return self.request("GET", "/api/v2!authuser")["Response"]["User"]["Uris"]["Node"]["Uri"]

    def list_albums(self):
        nickname = self.authuser()["NickName"]
        for a in self.user_albums(nickname):
            yield {"album_id": a.get("AlbumKey"), "ref": a.get("Uri"), "name": a.get("Name"),
                   "path": a.get("UrlPath"), "item_count": a.get("ImageCount"), "raw": a}

    def list_folder_albums(self, folder_path):
        """Albums under a folder given by display names ("A/B"; "" or "/" = the whole account), recursively.
        Never creates anything: a missing folder yields nothing."""
        node_uri = self.root_folder()
        parts = [p for p in folder_path.split("/") if p.strip()]
        for part in parts:
            node = self.find_child(node_uri, "Folder", part)
            if node is None:
                return
            node_uri = node["Uri"]
        stack = [(node_uri, "/".join(parts))]
        while stack:
            uri, folder = stack.pop()
            for node in self.node_children(uri):
                if node.get("Type") == "Folder":
                    stack.append((node["Uri"], f"{folder}/{node.get('Name')}" if folder else node.get("Name")))
                elif node.get("Type") == "Album":
                    album_uri = ((node.get("Uris") or {}).get("Album") or {}).get("Uri")
                    if album_uri:
                        yield {"album_id": album_uri.rsplit("/", 1)[-1], "ref": album_uri, "name": node.get("Name"),
                               "path": node.get("UrlPath"), "folder": folder, "item_count": None, "raw": node}

    def album_ref(self, album_id):
        return f"/api/v2/album/{album_id}"

    def item_ref(self, album_id, item_id, serial=0):
        """Reference to an item *in a specific album* (what moves and removals operate on)."""
        return f"/api/v2/album/{album_id}/image/{item_id}-{serial or 0}"

    def album_info(self, album_id):
        a = self.album(album_id)
        return {"album_id": album_id, "name": a.get("Name"), "url_name": a.get("UrlName"),
                "item_count": a.get("ImageCount", 0) or 0}

    def rename_album(self, album_id, name):
        """Change an album's display name. Never its UrlName: changing that breaks existing links (no redirect).
        Returns the name read back, since some PATCHes return OK without changing anything."""
        self.request("PATCH", f"/api/v2/album/{album_id}", json_body={"Name": name}, idempotent=True)
        return self.album(album_id).get("Name")

    def album_settings(self, album_id):
        """The album's settings in neutral names. Privacy is the album's own setting (from its node; Album.Privacy
        reports the effective value instead), and effective_privacy is what applies under its folders."""
        album = self.album(album_id)
        node = self.request("GET", album["Uris"]["Node"]["Uri"],
                            params={"_filter": "Privacy,EffectivePrivacy"})["Response"]["Node"]
        out = {}
        for name, (field, values) in ALBUM_SETTINGS.items():
            raw = node.get("Privacy") if name == "privacy" else album.get(field)
            if values:
                back = {v.lower(): k for k, v in values.items()}
                out[name] = back.get(str(raw).lower(), raw)
            else:
                out[name] = raw
        privacy_back = {v: k for k, v in ALBUM_SETTINGS["privacy"][1].items()}
        out["effective_privacy"] = privacy_back.get(node.get("EffectivePrivacy"), node.get("EffectivePrivacy"))
        return out

    def set_album_settings(self, album_id, changes):
        """Change album settings (neutral names and values); returns all settings read back, since some PATCHes
        return OK without changing anything. Downloads are switched first: the download size is ignored while
        they're off."""
        body = {}
        for name, value in changes.items():
            if name not in ALBUM_SETTINGS:
                raise ValueError(f"unknown album setting {name!r}")
            field, values = ALBUM_SETTINGS[name]
            if values:
                if value not in values:
                    raise ValueError(f"{name} must be one of {list(values)}, got {value!r}")
                body[field] = values[value]
            else:
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be true or false, got {value!r}")
                body[field] = value
        if "AllowDownloads" in body:
            self.request("PATCH", f"/api/v2/album/{album_id}", json_body={"AllowDownloads": body.pop("AllowDownloads")},
                         idempotent=True)
        if body:
            self.request("PATCH", f"/api/v2/album/{album_id}", json_body=body, idempotent=True)
        return self.album_settings(album_id)

    def album_items_page(self, album_id, start, count, with_metadata=False):
        """One page of an album's items (start is 1-based), e.g. to sample a large album."""
        params = {"start": start, "count": count}
        if with_metadata:
            params["_expand"] = "ImageMetadata"
        body = self.request("GET", f"/api/v2/album/{album_id}!images", params=params)
        expansions = body.get("Expansions", {})
        out = []
        for img in body.get("Response", {}).get("AlbumImage", []) or []:
            md_uri = img.get("Uris", {}).get("ImageMetadata", {}).get("Uri")
            md = expansions.get(md_uri, {}).get("ImageMetadata", {}) if md_uri else {}
            out.append(self._neutral_item(img, md))
        return out

    def album_item_count(self, album_id):
        return self.album(album_id).get("ImageCount", 0) or 0

    def list_album_items(self, album_id, with_metadata=False, ids_only=False):
        """Yield neutral item dicts for an album. ids_only fetches minimal fields (fast; only item_id is set)."""
        if ids_only:
            for img, _ in self.album_images(album_id, with_metadata=False, fields="ImageKey"):
                yield {"item_id": img.get("ImageKey")}
            return
        for img, md in self.album_images(album_id, with_metadata=with_metadata):
            yield self._neutral_item(img, md)

    def album_contains(self, album_id, item_id, serial=0):
        return self.album_has_image(album_id, item_id, serial)

    def item_album_ids(self, item_id, serial=0):
        return self.image_album_keys(item_id, serial)

    def collect_items(self, dest_album_id, item_refs):
        """Add items to another album without removing them from where they are (see docs: collect semantics).
        Not idempotent-retried: an ambiguous failure is checked against the server by the caller."""
        self.request("POST", f"/api/v2/album/{dest_album_id}!collectimages",
                     json_body={"CollectUris": ",".join(item_refs)})

    def move_items(self, dest_album_id, item_refs):
        """All-or-nothing on SmugMug: a rejected batch (HTTP 400) moves nothing."""
        self.move_images(dest_album_id, item_refs)

    def remove_item(self, item_ref):
        """Remove an item from the album its ref points into (deletes it if that's its only album)."""
        self.request("DELETE", item_ref)

    def upload_file(self, album_id, path, filename, content_type):
        """Upload; returns {'item_id', 'item_ref'}. Raises SmugMugError(ambiguous=True) if the outcome is unknown."""
        image = self.upload(self.album_ref(album_id), path, filename, content_type)
        return {"item_id": image["ImageUri"].rsplit("/", 1)[-1].split("-")[0], "item_ref": image.get("AlbumImageUri")}

    def preview_bytes(self, item_id, serial=0):
        """A small rendition of the item (for perceptual matching)."""
        sizes = self.request("GET", f"/api/v2/image/{item_id}-{serial or 0}!sizes")["Response"]["ImageSizes"]
        url = sizes.get("SmallImageUrl") or sizes.get("MediumImageUrl") or sizes.get("LargestImageUrl")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    def download_item(self, item_id, serial=0, is_video=False):
        """Bytes of the largest available rendition (fallback source when no original is available locally).
        Note: SmugMug re-encodes videos, so this is not the original file."""
        loc = "!largestvideo" if is_video else "!largestimage"
        body = self.request("GET", f"/api/v2/image/{item_id}-{serial or 0}{loc}")["Response"]
        url = (body.get("LargestVideo") or body.get("LargestImage") or {}).get("Url")
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        return r.content

    def video_info(self, item_id, serial=0):
        """{'duration_s', 'width', 'height'} of a video's largest rendition (None values if unavailable)."""
        body = self.request("GET", f"/api/v2/image/{item_id}-{serial or 0}!largestvideo")["Response"]
        v = body.get("LargestVideo") or {}
        return {"duration_s": self._duration_s(v.get("Duration")), "width": v.get("Width"), "height": v.get("Height")}

    def set_sort_by_filename(self, album_id):
        return self.set_album_sort(album_id, "FileName")

    def delete_folder(self, folder_ref):
        """Delete a folder node (and anything in it). Used for sandbox cleanup."""
        self.request("DELETE", folder_ref)

    def delete_album(self, album_key):
        self.request("DELETE", f"/api/v2/album/{album_key}")
