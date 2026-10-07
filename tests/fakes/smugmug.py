"""In-memory stand-in for the SmugMug client's neutral surface, with SmugMug's verified quirks.

albums: {album_id: set(item_id)}; items: {item_id: dict(name, md5, is_video, ...)}.
Quirk switches (all off by default):
  bad          item ids whose refs make a batch move fail with 400 (all-or-nothing)
  ambiguous    number of upcoming moves that are applied but then raise an ambiguous 504
  processing   item ids still processing: moves "succeed" (no error) but are silently ignored
  min_video_bytes  uploads of .mp4/.mov smaller than this are rejected (code 72), like tiny clips
"""

import hashlib
import itertools

from gp2sm.smugmug.client import SMUGMUG_CAPABILITIES, NotFound, SmugMugError


class FakeSmugMug:
    capabilities = SMUGMUG_CAPABILITIES

    def __init__(self, albums=None, items=None, bad=(), ambiguous=0, processing=(), min_video_bytes=0, folders=None):
        self.albums = {k: set(v) for k, v in (albums or {}).items()}
        self.folders = dict(folders or {})   # album_id -> display folder path ("" = top level)
        self.names = {}                      # album_id -> display name (default: the id)
        self.items = dict(items or {})
        self.bad, self.processing = set(bad), set(processing)
        self.ambiguous = ambiguous
        self.min_video_bytes = min_video_bytes
        self.calls = []
        self._ids = (f"N{n:05d}" for n in itertools.count(1))

    # refs
    @staticmethod
    def album_ref(album_id):
        return f"/api/v2/album/{album_id}"

    @staticmethod
    def item_ref(album_id, item_id, serial=0):
        return f"/api/v2/album/{album_id}/image/{item_id}-{serial or 0}"

    @staticmethod
    def _parse(ref):
        parts = ref.split("/")
        return parts[4], parts[6].split("-")[0]

    # reads
    def root_folder(self):
        return "/api/v2/node/ROOT"

    def ensure_folder_path(self, root, path):
        return f"/api/v2/node/{path}"

    def ensure_album(self, parent, name):
        album_id = "A_" + "".join(ch for ch in name if ch.isalnum())
        created = album_id not in self.albums
        self.albums.setdefault(album_id, set())
        if created:
            self.folders[album_id] = parent.split("/api/v2/node/", 1)[-1].replace("ROOT", "").strip("/")
            self.names[album_id] = name
        return album_id, self.album_ref(album_id), f"/api/v2/node/{album_id}", created

    def list_albums(self):
        for album_id, keys in self.albums.items():
            yield {"album_id": album_id, "ref": self.album_ref(album_id), "name": self.names.get(album_id, album_id),
                   "path": f"/{album_id}", "folder": self.folders.get(album_id, ""), "item_count": len(keys), "raw": {}}

    def list_folder_albums(self, folder_path):
        want = folder_path.strip("/")
        for album in self.list_albums():
            folder = album["folder"]
            if not want or folder == want or folder.startswith(want + "/"):
                yield dict(album, item_count=None)

    def album_item_count(self, album_id):
        if album_id not in self.albums:
            raise NotFound("no album", http_status=404)
        return len(self.albums[album_id])

    def list_album_items(self, album_id, with_metadata=False, ids_only=False):
        for item_id in sorted(self.albums[album_id]):
            it = self.items.get(item_id, {})
            yield {"item_id": item_id, "serial": 0, "item_ref": self.item_ref(album_id, item_id),
                   "name": it.get("name"), "md5": it.get("md5"), "is_video": it.get("is_video", False),
                   "size": it.get("size"), "width": it.get("width"), "height": it.get("height"),
                   "duration_s": it.get("duration_s"), "uploaded": None, "capture_time": None,
                   "format": None, "raw": {}, "raw_metadata": None}

    def album_contains(self, album_id, item_id, serial=0):
        return item_id in self.albums.get(album_id, ())

    def item_album_ids(self, item_id, serial=0):
        found = [a for a, keys in self.albums.items() if item_id in keys]
        if not found:
            raise NotFound("gone", http_status=404)
        return found

    # writes
    def move_items(self, dest, refs):
        self.calls.append(("move", dest, list(refs)))
        moves = []
        for ref in refs:
            src, item_id = self._parse(ref)
            if item_id in self.bad or item_id not in self.albums.get(src, ()):
                raise SmugMugError("bad ref", http_status=400)  # all-or-nothing: nothing moved
            moves.append((src, item_id))
        for src, item_id in moves:
            if item_id in self.processing:
                continue  # silently ignored, like an item still processing on SmugMug
            self.albums[src].discard(item_id)
            self.albums[dest].add(item_id)
        if self.ambiguous:
            self.ambiguous -= 1
            raise SmugMugError("504 after applying", http_status=504, ambiguous=True)

    def remove_item(self, ref):
        album_id, item_id = self._parse(ref)
        if item_id not in self.albums.get(album_id, ()):
            raise NotFound("not in album", http_status=404)
        self.albums[album_id].discard(item_id)

    def upload_file(self, album_id, path, filename, content_type):
        data = open(path, "rb").read()
        if filename.lower().endswith((".mp4", ".mov")) and len(data) < self.min_video_bytes:
            raise SmugMugError("stat=fail code=72 video too small", http_status=200, code=72)
        item_id = next(self._ids)
        self.items[item_id] = {"name": filename, "size": len(data), "is_video": content_type.startswith("video/"),
                               "data": data, "md5": hashlib.md5(data).hexdigest()}
        self.albums.setdefault(album_id, set()).add(item_id)
        return {"item_id": item_id, "item_ref": self.item_ref(album_id, item_id)}

    def preview_bytes(self, item_id, serial=0):
        return self.items.get(item_id, {}).get("preview", b"")

    def download_item(self, item_id, serial=0, is_video=False):
        return self.items[item_id].get("data", b"")

    def video_info(self, item_id, serial=0):
        it = self.items.get(item_id, {})
        return {"duration_s": it.get("duration_s"), "width": it.get("width"), "height": it.get("height")}

    def delete_folder(self, folder_ref):
        pass

    def delete_album(self, album_id):
        self.albums.pop(album_id, None)

    def set_sort_by_filename(self, album_id):
        self.calls.append(("sort", album_id))
