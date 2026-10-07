"""Decide what to upload, where, and under which name (pure; no I/O).

Inputs are plain dicts: the source's items, each item's effective dedupe decision, the destination snapshot,
what was planned before, the [takeout] policies, album settings and the destination's capabilities.

Per item:
  photo   exact/same -> nothing to upload (its copy on the destination still anchors its Live Photo clip)
          review     -> held ('needs_review'), and so is its clip
          new        -> uploaded into the month album of its capture date; HEIC and rejected formats are
                        converted to JPEG (or kept / skipped, per policy)
  video   regular videos (they have Google metadata) go to the video month album
  clip    Live Photo motion clips (attached by name, plus short videos with no metadata) are paired with a
          still by capture time and shape across all stills (`pairing.assign`), and uploaded beside it
          under the still's name, so a filename-sorted album shows each pair together
Names are made unique within each album (`name (1).JPG`), and albums over the soft cap continue as '- Part N'.
"""

import datetime
import os
from zoneinfo import ZoneInfo

from gp2sm.importer.pairing import assign
from gp2sm.media import aspect

LIVE_CLIP_MAX_S = 4.0     # a video with no metadata this short is treated as a Live Photo motion clip
HEIF = (".heic", ".heif", ".hif")
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
                 ".heic": "image/heic", ".heif": "image/heif", ".tif": "image/tiff", ".tiff": "image/tiff",
                 ".dng": "image/x-adobe-dng", ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4",
                 ".3gp": "video/3gpp"}


def to_epoch(iso):
    """ISO 8601 with an offset -> unix seconds; naive or missing -> None (can't be placed on a timeline)."""
    if not iso:
        return None
    dt = datetime.datetime.fromisoformat(iso)
    return int(dt.timestamp()) if dt.tzinfo else None


def local_month(ts, tz_name):
    """unix seconds -> ('YYYY', 'MM') in the project's time zone, or None."""
    if ts is None:
        return None
    dt = datetime.datetime.fromtimestamp(ts, ZoneInfo(tz_name))
    return f"{dt.year:04d}", f"{dt.month:02d}"


def album_for(is_video, ts, albums, tz_name):
    month = local_month(ts, tz_name)
    if month is None:
        return albums["undated_video"] if is_video else albums["undated_photo"]
    template = albums["video"] if is_video else albums["photo"]
    return template.format(yyyy=month[0], mm=month[1])


def still_output(name, policy, caps):
    """(upload name, convert_to_jpeg, skip_reason) for a photo."""
    stem, ext = os.path.splitext(name)
    low = ext.lower()
    if low in HEIF:
        if policy["heic"] == "convert":
            return stem + ".JPG", True, None
        return name, False, None
    if low in caps.rejected_extensions:
        if policy["rejected_types"] == "convert":
            return stem + ".JPG", True, None
        return name, False, f"{low} is rejected by the destination (rejected_types = skip)"
    return name, False, None


def landed_name(name, caps):
    """The name a file ends up with on the destination (it may convert on upload, e.g. HEIC -> JPG)."""
    stem, ext = os.path.splitext(name)
    for src, dst in caps.converts_on_upload:
        if ext.lower() == src:
            return stem + dst.upper()
    return name


class Albums:
    """Names taken and item counts per album (destination snapshot + this plan), with soft-cap parts."""

    def __init__(self, dest_names, dest_counts, soft_cap):
        self.names = {a: set(n) for a, n in dest_names.items()}
        self.counts = dict(dest_counts)
        self.soft_cap = soft_cap

    def part_for(self, base):
        n = 1
        while self.counts.get(self._part(base, n), 0) >= self.soft_cap:
            n += 1
        return self._part(base, n)

    @staticmethod
    def _part(base, n):
        return base if n == 1 else f"{base} - Part {n}"

    def claim(self, album, name, caps):
        """Reserve a unique name in the album (checking the name it lands with, too); returns it."""
        taken = self.names.setdefault(album, set())
        stem, ext = os.path.splitext(name)
        final, k = name, 0
        while final.lower() in taken or landed_name(final, caps).lower() in taken:
            k += 1
            final = f"{stem} ({k}){ext}"
        taken.add(final.lower())
        taken.add(landed_name(final, caps).lower())
        self.counts[album] = self.counts.get(album, 0) + 1
        return final


def plan(items, decisions, dest, previous, policy, albums_cfg, tz_name, caps, soft_cap):
    """items: [dict(ref, name, kind, taken_ts, own_time, width, height, duration_s, orphan,
                    motion: dict(ref, name, own_time, width, height, duration_s) or None)]
    decisions: {ref: dict(decision, dest_item_id)}  (effective decisions)
    dest: dict(items={item_id: dict(album, album_id, name, is_video)}, names={album: set(lowercase names)},
               counts={album: n})   (album = album name)
    previous: {(ref, role): dict(target_name, upload_name)}  rows already planned (kept as they are)
    Returns (rows, notes): rows are dict(source_ref, role, target_name, upload_name, convert, content_type,
    taken_ts, pair_ref, album_id, reason, status); album_id is set when the target is a specific existing album
    (a clip joining its still's album); notes counts what was skipped and why."""
    albums = Albums(dest["names"], dest["counts"], soft_cap)
    for p in previous.values():
        albums.names.setdefault(p["target_name"], set()).add(p["upload_name"].lower())
    rows, notes = [], {}

    def note(key):
        notes[key] = notes.get(key, 0) + 1

    def add(ref, role, target, name, convert, ts, reason, status="planned", pair_ref=None, album_id=None):
        ext = os.path.splitext(name)[1].lower()
        rows.append({"source_ref": ref, "role": role, "target_name": target, "upload_name": name,
                     "convert": convert, "content_type": CONTENT_TYPES.get(ext, "application/octet-stream"),
                     "taken_ts": ts, "pair_ref": pair_ref, "album_id": album_id, "reason": reason, "status": status})

    ordered = sorted(items, key=lambda i: (i["taken_ts"] or to_epoch(i.get("own_time")) or 0, i["name"], i["ref"]))
    anchors = {}   # still ref -> (album, still name on the destination, held?, existing album id or None)
    clips = []     # clip candidates for pairing

    for it in ordered:
        ref, decision = it["ref"], decisions.get(it["ref"], {}).get("decision", "new")
        if decision == "source_duplicate":
            note("same file appears twice in the source")
            continue
        ts = it["taken_ts"] or to_epoch(it.get("own_time"))
        if it["kind"] == "photo":
            if decision in ("exact", "same"):
                d = dest["items"].get(decisions[ref].get("dest_item_id"))
                if d:
                    anchors[ref] = (d["album"], d["name"], False, d.get("album_id") or True)
                note(f"already on the destination ({decision})")
            else:
                prev = previous.get((ref, "still"))
                if prev:
                    anchors[ref] = (prev["target_name"], prev["upload_name"], decision == "review", None)
                else:
                    name, convert, skip = still_output(it["name"], policy, caps)
                    if skip:
                        note(skip)
                    else:
                        target = albums.part_for(album_for(False, ts, albums_cfg, tz_name))
                        final = albums.claim(target, name, caps)
                        held = decision == "review"
                        add(ref, "still", target, final, convert, ts,
                            "close to a same-named copy on the destination; check it" if held else "new photo",
                            "needs_review" if held else "planned")
                        anchors[ref] = (target, final, held, None)
            if it.get("motion"):
                clips.append(dict(it["motion"], still_ref=ref, name_ts=ts))
        elif it.get("orphan") and it.get("duration_s") and it["duration_s"] <= LIVE_CLIP_MAX_S:
            if decision in ("exact", "same"):
                note(f"already on the destination ({decision})")
                continue
            clips.append({"ref": ref, "name": it["name"], "own_time": it.get("own_time"), "width": it.get("width"),
                          "height": it.get("height"), "duration_s": it.get("duration_s"), "still_ref": None,
                          "name_ts": None})
        elif decision in ("exact", "same"):
            note(f"already on the destination ({decision})")
        elif (ref, "video") not in previous:
            target = albums.part_for(album_for(True, ts, albums_cfg, tz_name))
            held = decision == "review"
            add(ref, "video", target, albums.claim(target, it["name"], caps), False, ts,
                "same-named video on the destination couldn't be compared" if held else "new video",
                "needs_review" if held else "planned")

    if policy["live_clips"] == "skip":
        notes["Live Photo clips skipped (live_clips = skip)"] = len(clips)
        return rows, notes

    # pair clips with stills by time and shape
    stills = []
    for order, it in enumerate(ordered):
        if it["kind"] == "photo" and it["ref"] in anchors and it.get("width") and it.get("height"):
            ts = to_epoch(it.get("own_time")) or it["taken_ts"]
            if ts is not None:
                stills.append({"id": it["ref"], "ts": ts, "ratio": aspect(it["width"], it["height"]), "order": order})
    candidates = []
    for order, c in enumerate(clips):
        ts = to_epoch(c.get("own_time")) or c["name_ts"]
        if ts is not None and c.get("width") and c.get("height"):
            candidates.append({"id": c["ref"], "ts": ts, "ratio": aspect(c["width"], c["height"]), "order": order})
    paired = assign(candidates, stills, policy["pair_window"], policy["aspect_tolerance_pct"] / 100)

    for c in clips:
        ref = c["ref"]
        if (ref, "clip") in previous:
            continue
        ext = os.path.splitext(c["name"])[1].upper()
        ts = to_epoch(c.get("own_time")) or c["name_ts"]
        if ref in paired and policy["live_clips"] == "pair":
            still_ref, gap, _ = paired[ref]
            album, still_name, held, existing = anchors[still_ref]
            stem = os.path.splitext(still_name)[0]
            if existing and any(f"{stem}{e}".lower() in dest["names"].get(album, ()) for e in (".mp4", ".mov")):
                note("clip already beside its still")
                continue
            final = albums.claim(album, stem + ext, caps)
            add(ref, "clip", album, final, False, ts,
                f"Live Photo clip, {gap}s from its still" + ("" if c["still_ref"] == still_ref else " (paired by time)"),
                "needs_review" if held else "planned", pair_ref=still_ref,
                album_id=existing if isinstance(existing, str) else None)
        elif ref in paired:   # live_clips = separate
            target = albums.part_for(album_for(True, ts, albums_cfg, tz_name))
            add(ref, "clip", target, albums.claim(target, c["name"], caps), False, ts, "Live Photo clip (separate)")
        elif policy["unpaired_clips"] == "skip":
            note("clip with no matching still skipped (unpaired_clips = skip)")
        else:
            undated = policy["unpaired_clips"] == "undated"
            target = albums_cfg["undated_video"] if undated else albums.part_for(album_for(True, ts, albums_cfg, tz_name))
            add(ref, "clip", target, albums.claim(target, c["name"], caps), False, ts, "clip with no matching still")
    return rows, notes
