"""Google Takeout as a PhotoSource.

Built on the Takeout index (takeout/index.py) and its sidecar/Live Photo pairing (takeout/items.py build_items);
independent of any destination or legacy database. Items are yielded in a stable order. Reading bytes
streams the archive (.zip or .tgz); prefer iter_bytes() for many items (one pass per archive).
"""

import os
import sqlite3
from collections import defaultdict

from gp2sm.services.base import SourceItem
from gp2sm.takeout.archive import read_members
from gp2sm.takeout.items import MOTION_EXTS, build_items


def _ref(archive, path):
    return f"{archive}::{path}"


def _facts(member):
    """The file's own facts recorded by the index (absent in indexes made before they were recorded)."""
    return {k: member.get(k) for k in ("width", "height", "duration_s", "own_time")}


def _split(ref):
    archive, path = ref.split("::", 1)
    return archive, path


class TakeoutSource:
    name = "google-takeout"

    def __init__(self, index_db, takeout_dir):
        self.index_db = index_db
        self.takeout_dir = takeout_dir

    def _items(self):
        conn = sqlite3.connect(f"file:{os.path.abspath(self.index_db)}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            items, orphans = build_items(conn)
        finally:
            conn.close()
        return items, orphans

    def iter_items(self, include_orphans=False):
        """SourceItems for every media file with a metadata sidecar (optionally also orphan media)."""
        items, orphans = self._items()
        out = []
        for it in items:
            m, js, mo = it["media"], it["js"], it.get("motion")
            if m is None:
                continue
            taken = int(js.get("photoTakenTime", {}).get("timestamp") or 0) or None
            out.append(SourceItem(
                source_id=_ref(m["archive"], m["path"]), name=m["basename"],
                kind="video" if m["ext"] in MOTION_EXTS else "photo", taken_ts=taken,
                size=m["size"], md5=m["md5"], **_facts(m),
                motion_ref=_ref(mo["archive"], mo["path"]) if mo else None,
                extras={"title": js.get("title"), "sidecar": _ref(it["sidecar"]["archive"], it["sidecar"]["path"]),
                        "geo": js.get("geoData"), "url": js.get("url"),
                        "motion": dict(name=mo["basename"], **_facts(mo)) if mo else None,
                        "created_ts": int(js.get("creationTime", {}).get("timestamp") or 0) or None}))
        if include_orphans:
            for o in orphans:
                out.append(SourceItem(source_id=_ref(o["archive"], o["path"]), name=o["basename"],
                                      kind="video" if o["ext"] in MOTION_EXTS else "photo", taken_ts=None,
                                      size=o["size"], md5=o["md5"], extras={"orphan": True}, **_facts(o)))
        yield from sorted(out, key=lambda i: i.source_id)

    def open_ref(self, ref):
        """Bytes of one archive member (streams the archive until found)."""
        for _, data in self.iter_bytes_refs([ref]):
            return data
        raise FileNotFoundError(ref)

    def open_bytes(self, item):
        return self.open_ref(item.source_id)

    def iter_bytes_refs(self, refs):
        """Yield (ref, bytes) for many refs with one streaming pass per archive (archive order, not input order)."""
        wanted = defaultdict(set)
        for ref in refs:
            archive, path = _split(ref)
            wanted[archive].add(path)
        for archive in sorted(wanted):
            for path, data in read_members(os.path.join(self.takeout_dir, archive), wanted[archive]):
                yield _ref(archive, path), data

    def iter_bytes(self, items):
        by_ref = {i.source_id: i for i in items}
        for ref, data in self.iter_bytes_refs(by_ref):
            yield by_ref[ref], data
