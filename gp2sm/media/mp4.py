"""MP4/MOV header parsing (dimensions, duration, capture time), without any video library.

Reads the box structure: the top-level boxes are walked to find `moov` (which iPhone MOVs put at the *end*,
after the media data), and only `moov` is searched. Never search the whole file for a box name: compressed
media data contains such byte sequences by chance (a real MOV had a stray 'mvhd' 14.8 MB into its data).

Large files can be read as head + tail (`Partial`); the box walk jumps over the middle.
"""

import datetime
import re
import struct

QT_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01
APPLE_KEY = b"com.apple.quicktime.creationdate"
APPLE_DATE = re.compile(rb"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)([+-]\d{4}|Z)")


class Partial:
    """The first and last bytes of a file of `total` bytes (the middle wasn't read)."""

    def __init__(self, head, tail, total):
        self.head, self.tail, self.total = bytes(head), bytes(tail), total

    def read(self, offset, n):
        if offset + n <= len(self.head):
            return self.head[offset:offset + n]
        start = self.total - len(self.tail)
        if offset >= start and offset + n <= self.total:
            return self.tail[offset - start:offset - start + n]
        return None


def _reader(data):
    if isinstance(data, Partial):
        return data.read, data.total
    return (lambda off, n: data[off:off + n] if off + n <= len(data) else None), len(data)


def moov(data):
    """The `moov` box payload, or None if the file's box structure doesn't lead to one."""
    read, total = _reader(data)
    off = 0
    while off + 8 <= total:
        hdr = read(off, 8)
        if hdr is None:
            return None
        size, kind = struct.unpack(">I4s", hdr)
        head_len = 8
        if size == 1:
            big = read(off + 8, 8)
            if big is None:
                return None
            size, head_len = struct.unpack(">Q", big)[0], 16
        elif size == 0:
            size = total - off
        if size < head_len:
            return None   # not a box structure
        if kind == b"moov":
            return read(off + head_len, size - head_len)
        off += size
    return None


def children(payload):
    """(type, payload) of the boxes directly inside a container payload."""
    off = 0
    while off + 8 <= len(payload):
        size, kind = struct.unpack(">I4s", payload[off:off + 8])
        head_len = 8
        if size == 1:
            size, head_len = struct.unpack(">Q", payload[off + 8:off + 16])[0], 16
        elif size == 0:
            size = len(payload) - off
        if size < head_len or off + size > len(payload):
            return
        yield kind, payload[off + head_len:off + size]
        off += size


def _child(payload, kind):
    return next((p for k, p in children(payload) if k == kind), None)


def _mvhd(data):
    m = moov(data)
    return _child(m, b"mvhd") if m else None


def mp4_dims(data):
    """(width, height) of the first video track (from its 'tkhd'; audio tracks have 0x0), or None."""
    m = moov(data)
    if not m:
        return None
    for kind, trak in children(m):
        if kind != b"trak":
            continue
        tkhd = _child(trak, b"tkhd")
        if not tkhd:
            continue
        off = 4 + (32 if tkhd[0] == 1 else 20) + 8 + 8 + 36
        if len(tkhd) < off + 8:
            continue
        w = int.from_bytes(tkhd[off:off + 4], "big") >> 16
        h = int.from_bytes(tkhd[off + 4:off + 8], "big") >> 16
        if w and h:
            return w, h
    return None


def aspect(w, h):
    """Orientation-independent aspect ratio (long side / short side)."""
    return max(w, h) / min(w, h)


def mp4_duration(data):
    """Duration in seconds from the movie header ('moov/mvhd'), or None."""
    mvhd = _mvhd(data)
    if not mvhd:
        return None
    if mvhd[0] == 1:
        timescale, duration = struct.unpack(">IQ", mvhd[20:32])
    else:
        timescale, duration = struct.unpack(">II", mvhd[12:20])
    return duration / timescale if timescale else None


def mp4_creation_ts(data):
    """Unix creation time from 'moov/mvhd' (None if absent or zero)."""
    mvhd = _mvhd(data)
    if not mvhd:
        return None
    raw = struct.unpack(">Q", mvhd[4:12])[0] if mvhd[0] == 1 else struct.unpack(">I", mvhd[4:8])[0]
    return raw - QT_EPOCH_OFFSET if raw else None


def clip_creation_ts(data):
    """Capture time of a clip: Apple's com.apple.quicktime.creationdate (in moov's metadata) if present,
    the true capture time; else the mvhd creation time (which can be a later re-encode time)."""
    m = moov(data)
    if m and APPLE_KEY in m:
        match = APPLE_DATE.search(m, m.index(APPLE_KEY))
        if match:
            text = match.group(0).decode()
            text = text.replace("Z", "+00:00") if text.endswith("Z") else text[:-2] + ":" + text[-2:]
            return int(datetime.datetime.fromisoformat(text).timestamp())
    return mp4_creation_ts(data)
