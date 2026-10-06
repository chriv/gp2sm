"""Small media helpers that don't depend on any photo service."""


def mp4_dims(data):
    """(width, height) of the first video track, read from MP4/MOV 'tkhd' boxes (audio tracks have 0x0)."""
    i = 0
    while True:
        i = data.find(b"tkhd", i + 1)
        if i < 0:
            return None
        version = data[i + 4]
        off = i + 8 + (32 if version == 1 else 20) + 8 + 8 + 36
        w = int.from_bytes(data[off:off + 4], "big") >> 16
        h = int.from_bytes(data[off + 4:off + 8], "big") >> 16
        if w and h:
            return w, h


def aspect(w, h):
    """Orientation-independent aspect ratio (long side / short side)."""
    return max(w, h) / min(w, h)


def mp4_duration(data):
    """Duration in seconds from the MP4/MOV 'mvhd' box (None if absent)."""
    i = data.find(b"mvhd")
    if i < 0:
        return None
    version = data[i + 4]
    if version == 1:
        timescale = int.from_bytes(data[i + 24:i + 28], "big")
        duration = int.from_bytes(data[i + 28:i + 36], "big")
    else:
        timescale = int.from_bytes(data[i + 16:i + 20], "big")
        duration = int.from_bytes(data[i + 20:i + 24], "big")
    return duration / timescale if timescale else None
