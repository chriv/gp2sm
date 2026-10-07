"""Snapshot the destination albums in scope into dest_albums/dest_items (neutral client surface only)."""

import concurrent.futures
import logging

from gp2sm.state import now

log = logging.getLogger("gp2sm.importer.inventory")


def albums_in_scope(client, paths):
    """Albums under any of the given paths (display names; "/" = whole account), each once. A path may also
    name a single album ("Folder/Album"): when no folder has that path, its last part is taken as an album."""
    seen = {}
    for path in paths:
        found = list(client.list_folder_albums(path))
        if not found and path.strip("/"):
            parent, _, name = path.strip("/").rpartition("/")
            found = [a for a in client.list_folder_albums(parent) if a["name"] == name and a["folder"] == parent]
        for album in found:
            seen.setdefault(album["album_id"], album)
    return list(seen.values())


def refresh(st, client, folders, workers=4, progress=None):
    """List every album in scope and replace its rows. Albums no longer in scope are dropped from the snapshot."""
    albums = albums_in_scope(client, folders)
    log.info("inventory: %d albums under %s", len(albums), folders)
    if progress:
        progress.total = len(albums)

    def listing(album):  # worker thread: network only
        return album, list(client.list_album_items(album["album_id"]))

    items_total = 0
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for album, items in pool.map(listing, albums):
            with st.db:
                st.db.execute("DELETE FROM dest_items WHERE album_id=?", (album["album_id"],))
                st.db.executemany(
                    "INSERT OR REPLACE INTO dest_items(item_id, album_id, serial, name, md5, size, width, height, "
                    "is_video, duration_s) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(it["item_id"], album["album_id"], it.get("serial") or 0, it.get("name"), it.get("md5"),
                      it.get("size"), it.get("width"), it.get("height"), int(bool(it.get("is_video"))),
                      it.get("duration_s")) for it in items])
                st.db.execute("INSERT OR REPLACE INTO dest_albums VALUES(?,?,?,?,?)",
                              (album["album_id"], album.get("name"), album.get("folder"), len(items), now()))
            items_total += len(items)
            if progress:
                progress.update()
    keep = {a["album_id"] for a in albums}
    gone = [r[0] for r in st.q("SELECT album_id FROM dest_albums") if r[0] not in keep]
    with st.db:
        for album_id in gone:
            st.db.execute("DELETE FROM dest_items WHERE album_id=?", (album_id,))
            st.db.execute("DELETE FROM dest_albums WHERE album_id=?", (album_id,))
    return {"albums": len(albums), "items": items_total, "albums_dropped": len(gone)}
