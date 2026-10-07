"""Live Photo clip <-> still pairing by capture time and shape (pure).

Validated on a real library: 143 of 149 clips held back by name-based pairing were re-paired this way, 135
within 1 s, including clips that name-based pairing had swapped (reused names such as `lp_image`, `IMG_####`).
"""

import bisect


def assign(clips, stills, window=60, aspect_tol=0.02):
    """One-to-one clip -> still assignment by capture time and aspect ratio.

    clips/stills: lists of dict(id, ts, ratio, order). Returns {clip_id: (still_id, seconds_apart, candidates)}.
    Pairs are taken by smallest time gap; equal gaps are taken in (clip order, still order), so a burst of
    clips sharing one second is matched to that second's stills in name order.
    """
    stills = sorted(stills, key=lambda s: s["ts"])
    times = [s["ts"] for s in stills]
    pairs, ncand = [], {}
    for c in clips:
        lo, hi = bisect.bisect_left(times, c["ts"] - window), bisect.bisect_right(times, c["ts"] + window)
        cands = [s for s in stills[lo:hi] if abs(s["ratio"] - c["ratio"]) / s["ratio"] <= aspect_tol]
        ncand[c["id"]] = len(cands)
        pairs += [(abs(s["ts"] - c["ts"]), c["order"], s["order"], c["id"], s["id"]) for s in cands]
    pairs.sort()
    used_c, used_s, out = set(), set(), {}
    for dt, _, _, cid, sid in pairs:
        if cid in used_c or sid in used_s:
            continue
        used_c.add(cid)
        used_s.add(sid)
        out[cid] = (sid, dt, ncand[cid])
    return out
