"""Perceptual hashing: a 64-bit difference hash (dHash) of a small grayscale rendering.

Distances (Hamming, 0-64) calibrated on real libraries: 0-6 is the same photo (re-encoded, resized, or
converted), 19+ is a different photo; between is genuinely unclear. Rotation hashes cover sources whose
orientation handling differs (EXIF Orientation applied or not).
"""

from PIL import Image

from gp2sm.media.convert import render_small


def dhash(img):
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = g.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def rotation_hashes(img):
    """dHash of the image at 0/90/180/270 degrees (orientation handling differs between sources)."""
    return [dhash(img.rotate(a, expand=True)) for a in (0, 90, 180, 270)]


def hamming(a, b):
    return bin(a ^ b).count("1")


def bytes_hashes(data):
    """Image bytes (any format Pillow + pillow-heif opens) -> 4 rotation dHashes."""
    return rotation_hashes(render_small(data))


def bytes_hash(data):
    """Image bytes -> one dHash (for a destination's own rendition, already upright)."""
    return dhash(render_small(data))
