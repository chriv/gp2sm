"""Is each source item already on the destination?  exact | same | new | review | source_duplicate

Rules (pure: `decide`):
  1. Identical files inside the source (same MD5, e.g. one photo in both a year folder and an album folder)
     are decided once; the others are `source_duplicate` of the first.
  2. `exact`: a destination item has the same MD5 (only possible where the destination keeps original bytes).
  3. Photos, `content` mode: candidates are destination photos with the same base name (`IMG_1.HEIC` ~
     `IMG_1.JPG`, `x(2).jpg` ~ `x.jpg`). Pairs are taken closest first, one-to-one (burst frames sharing a
     name each claim at most one copy). Distance <= same_max: `same`. Otherwise, if every unclaimed candidate is
     >= different_min (or none is left): `new`; anything in between: `review`, never decided automatically.
  4. Videos, `content` mode: same base name and the same shape (duration within max(0.5 s, 7%), aspect ratio,
     and not larger than the source, since destinations don't upscale): `same`, one-to-one, closest duration
     first. Same-named videos of a different shape: `new`. Missing facts: `review`.
  A person's review answer (`reviewed`: same | different) overrides the automatic decision.
"""

import concurrent.futures
import logging
import os
import re

from gp2sm.media import aspect
from gp2sm.media.phash import bytes_hash, bytes_hashes, hamming
from gp2sm.state import now

log = logging.getLogger("gp2sm.importer.dedupe")

DECISIONS = ("exact", "same", "new", "review", "source_duplicate")


def group_key(name):
    """Lowercase base name without extension and without a trailing '(N)' / ' (N)' counter."""
    stem = os.path.splitext(name or "")[0].lower()
    return re.sub(r"\s?\(\d+\)$", "", stem)


def _decision(decision, method, dest=None, dist=None, detail=None):
    return {"decision": decision, "method": method, "dest_item_id": dest["item_id"] if dest else None,
            "dest_album_id": dest["album_id"] if dest else None, "dist": dist, "detail": detail}


def representatives(sources):
    """(unique sources, {duplicate ref: representative ref}) by MD5; the smallest ref represents each group."""
    first, dup_of = {}, {}
    for s in sorted(sources, key=lambda s: s["ref"]):
        if s.get("md5") and s["md5"] in first:
            dup_of[s["ref"]] = first[s["md5"]]["ref"]
        else:
            first.setdefault(s.get("md5") or ("ref", s["ref"]), s)
    return list(first.values()), dup_of


def content_candidates(sources, dests):
    """{source ref: [dest item_id]} for photos that need a content comparison (no MD5 match, same base name)."""
    by_md5 = {d["md5"] for d in dests if d.get("md5")}
    by_group = {}
    for d in dests:
        if not d.get("is_video"):
            by_group.setdefault(group_key(d["name"]), []).append(d["item_id"])
    unique, _ = representatives(sources)
    return {s["ref"]: by_group[group_key(s["name"])] for s in unique
            if s["kind"] == "photo" and s.get("md5") not in by_md5 and group_key(s["name"]) in by_group}


def video_distance(src, dst, aspect_tol):
    """Duration difference if dst has src's shape; None if it doesn't; 'unknown' if facts are missing."""
    if not src.get("duration_s") or not dst.get("duration_s"):
        return "unknown"
    gap = abs(src["duration_s"] - dst["duration_s"])
    if gap > max(0.5, 0.07 * src["duration_s"]):
        return None
    if src.get("width") and src.get("height") and dst.get("width") and dst.get("height"):
        a, b = aspect(src["width"], src["height"]), aspect(dst["width"], dst["height"])
        if abs(a - b) / a > aspect_tol:
            return None
        if dst["width"] * dst["height"] > src["width"] * src["height"] * 1.01:
            return None
    return gap


def decide(sources, dests, src_hashes, dest_hashes, *, mode="content", same_max=6, different_min=19,
           aspect_tol=0.02):
    """sources: [dict(ref, name, kind, md5, width, height, duration_s)];
    dests: [dict(item_id, album_id, name, md5, is_video, width, height, duration_s)];
    src_hashes: {ref: [4 rotation hashes] or None}; dest_hashes: {item_id: hash or None}.
    Returns {ref: dict(decision, method, dest_item_id, dest_album_id, dist, detail)}."""
    unique, dup_of = representatives(sources)
    out = {ref: _decision("source_duplicate", "md5", detail=f"same file as {rep}") for ref, rep in dup_of.items()}
    if mode == "off":
        out.update({s["ref"]: _decision("new", "off") for s in unique})
        return out
    by_md5 = {}
    for d in dests:
        if d.get("md5"):
            by_md5.setdefault(d["md5"], d)
    rest = []
    for s in unique:
        d = by_md5.get(s.get("md5"))
        if d:
            out[s["ref"]] = _decision("exact", "md5", d, 0)
        else:
            rest.append(s)
    if mode == "exact":
        out.update({s["ref"]: _decision("new", "md5") for s in rest})
        return out

    dest_by_id = {d["item_id"]: d for d in dests}
    groups = {}
    for d in dests:
        groups.setdefault((bool(d.get("is_video")), group_key(d["name"])), []).append(d)
    photos = [s for s in rest if s["kind"] == "photo"]
    videos = [s for s in rest if s["kind"] != "photo"]

    # photos: closest-first, one-to-one
    scored, unknown = {}, set()
    for s in photos:
        cands = groups.get((False, group_key(s["name"])), [])
        hs = src_hashes.get(s["ref"])
        scored[s["ref"]] = []
        for d in cands:
            h = dest_hashes.get(d["item_id"])
            if hs is None or h is None:
                unknown.add(s["ref"])
                continue
            scored[s["ref"]].append((min(hamming(x, h) for x in hs), d["item_id"]))
    pairs = sorted((dist, ref, item_id) for ref, c in scored.items() for dist, item_id in c if dist <= same_max)
    used = set()
    for dist, ref, item_id in pairs:
        if ref in out or item_id in used:
            continue
        used.add(item_id)
        out[ref] = _decision("same", "content", dest_by_id[item_id], dist)
    for s in photos:
        ref = s["ref"]
        if ref in out:
            continue
        left = sorted((dist, item_id) for dist, item_id in scored[ref] if item_id not in used)
        nearest = left[0] if left else None
        if ref in unknown and (not nearest or nearest[0] > same_max):
            out[ref] = _decision("review", "content", dest_by_id[nearest[1]] if nearest else None,
                                 nearest[0] if nearest else None, "a same-named copy couldn't be compared")
        elif not left or nearest[0] >= different_min:
            detail = (f"nearest same-named copy differs (distance {nearest[0]})" if nearest
                      else "same-named copies already claimed by closer matches" if scored[ref] else None)
            out[ref] = _decision("new", "content" if scored[ref] else "none",
                                 dist=nearest[0] if nearest else None, detail=detail)
        else:
            out[ref] = _decision("review", "content", dest_by_id[nearest[1]], nearest[0],
                                 f"distance {nearest[0]} is between {same_max} and {different_min}")

    # videos: same shape, one-to-one by closest duration
    vpairs, vunknown, vnamed = [], {}, set()
    for s in videos:
        for d in groups.get((True, group_key(s["name"])), []):
            vnamed.add(s["ref"])
            gap = video_distance(s, d, aspect_tol)
            if gap == "unknown":
                vunknown.setdefault(s["ref"], d)
            elif gap is not None:
                vpairs.append((gap, s["ref"], d["item_id"]))
    for gap, ref, item_id in sorted(vpairs):
        if ref in out or item_id in used:
            continue
        used.add(item_id)
        out[ref] = _decision("same", "video_shape", dest_by_id[item_id], None, f"duration differs by {gap:.2f}s")
    for s in videos:
        ref = s["ref"]
        if ref in out:
            continue
        if ref in vunknown:
            out[ref] = _decision("review", "video_shape", vunknown[ref], None, "duration unknown; can't compare")
        else:
            out[ref] = _decision("new", "video_shape" if ref in vnamed else "none",
                                 detail="same-named video has a different shape" if ref in vnamed else None)
    return out


# ---------------------------------------------------------------------------------------------- I/O

def _cached_source_hashes(st, refs):
    out = {}
    for ref in refs:
        row = st.db.execute("SELECT h0, h1, h2, h3, error FROM hash_source WHERE source_ref=?", (ref,)).fetchone()
        if row:
            out[ref] = None if row[4] else [int(h, 16) for h in row[:4]]
    return out


def _cached_dest_hashes(st, ids):
    out = {}
    for item_id in ids:
        row = st.db.execute("SELECT h, error FROM hash_dest WHERE item_id=?", (item_id,)).fetchone()
        if row:
            out[item_id] = None if row[1] else int(row[0], 16)
    return out


def bounded_map(pool, fn, pairs, limit):
    """Yield (key, future) for (key, arg) pairs, keeping at most `limit` submitted but unfinished, so a fast
    producer (an archive being read) can't pile up unprocessed data in memory."""
    inflight = {}
    for key, arg in pairs:
        inflight[pool.submit(fn, arg)] = key
        if len(inflight) >= limit:
            done, _ = concurrent.futures.wait(inflight, return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                yield inflight.pop(fut), fut
    for fut in concurrent.futures.as_completed(list(inflight)):
        yield inflight.pop(fut), fut


def hash_sources(st, refs, read_refs, workers=4, progress=None):
    """Hashes for source refs, computing (and caching, as it goes) the missing ones.
    read_refs(refs) yields (ref, bytes). Resumable: an interrupted run keeps what it hashed."""
    have = _cached_source_hashes(st, refs)
    todo = [r for r in refs if r not in have]
    if todo:
        log.info("hashing %d source photos (%d cached)", len(todo), len(have))
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            for n, (ref, fut) in enumerate(bounded_map(pool, bytes_hashes, read_refs(todo), workers * 2), 1):
                try:
                    hs = fut.result()
                    st.db.execute("INSERT OR REPLACE INTO hash_source VALUES(?,?,?,?,?,NULL)",
                                  (ref, *[f"{h:016x}" for h in hs]))
                    have[ref] = hs
                except Exception as e:  # unreadable: recorded, and the item goes to review
                    st.db.execute("INSERT OR REPLACE INTO hash_source(source_ref, error) VALUES(?,?)", (ref, repr(e)))
                    have[ref] = None
                if n % 200 == 0:
                    st.db.commit()
                if progress:
                    progress.update()
        st.db.commit()
    return have


def hash_dests(st, client, ids, workers=4, progress=None):
    """Hashes for destination items, computing (and caching) the missing ones from small renditions."""
    have = _cached_dest_hashes(st, ids)
    todo = [i for i in ids if i not in have]
    if todo:
        log.info("hashing %d destination photos (%d cached)", len(todo), len(have))
        serial = dict(st.q(f"SELECT item_id, serial FROM dest_items WHERE item_id IN ({','.join('?' * len(todo))})",
                           *todo))

        def one(item_id):  # worker thread: network + CPU only
            return bytes_hash(client.preview_bytes(item_id, serial.get(item_id) or 0))

        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            for n, (item_id, fut) in enumerate(bounded_map(pool, one, ((i, i) for i in todo), workers * 4), 1):
                try:
                    h = fut.result()
                    st.db.execute("INSERT OR REPLACE INTO hash_dest VALUES(?,?,NULL)", (item_id, f"{h:016x}"))
                    have[item_id] = h
                except Exception as e:
                    st.db.execute("INSERT OR REPLACE INTO hash_dest(item_id, error) VALUES(?,?)", (item_id, repr(e)))
                    have[item_id] = None
                if n % 200 == 0:
                    st.db.commit()
                if progress:
                    progress.update()
        st.db.commit()
    return have


def fill_video_facts(st, client, sources, dests, workers=4):
    """Destination listings may lack video durations; fetch them only for same-named video candidates (cached)."""
    by_md5 = {d["md5"] for d in dests if d.get("md5")}
    wanted = {group_key(s["name"]) for s in sources if s["kind"] != "photo" and s.get("md5") not in by_md5}
    todo = [d for d in dests if d.get("is_video") and d.get("duration_s") is None and group_key(d["name"]) in wanted]
    if not todo:
        return 0
    log.info("reading %d destination video details", len(todo))

    def one(d):  # worker thread: network only
        try:
            return d, client.video_info(d["item_id"])
        except Exception as e:
            log.warning("video details for %s: %r", d["item_id"], e)
            return d, {}

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, todo))
    with st.db:
        for d, info in results:
            st.db.execute("UPDATE dest_items SET duration_s=?, width=COALESCE(width, ?), height=COALESCE(height, ?) "
                          "WHERE item_id=?", (info.get("duration_s"), info.get("width"), info.get("height"),
                                              d["item_id"]))
    return len(todo)


def dest_items(st):
    return [dict(r) for r in st.q("SELECT item_id, album_id, name, md5, is_video, width, height, duration_s "
                                  "FROM dest_items")]


def run_progress(total, label):
    from gp2sm.cli.run import Progress
    return Progress(total, label)


def run(st, client, sources, read_refs, policy, workers=4):
    """Decide every source item against the inventory and record it in source_matches. Returns counts."""
    dests = dest_items(st)
    mode = policy["dedupe"]
    src_h, dst_h = {}, {}
    if mode == "content":
        if fill_video_facts(st, client, sources, dests, workers):
            dests = dest_items(st)
        needed = content_candidates(sources, dests)
        dest_ids = sorted({i for ids in needed.values() for i in ids})
        progress = run_progress(len(needed), "hashing source photos")
        src_h = hash_sources(st, sorted(needed), read_refs, workers, progress)
        progress.close()
        progress = run_progress(len(dest_ids), "hashing destination photos")
        dst_h = hash_dests(st, client, dest_ids, workers, progress)
        progress.close()
    decisions = decide(sources, dests, src_h, dst_h, mode=mode, same_max=policy["same_max"],
                       different_min=policy["different_min"], aspect_tol=policy["aspect_tolerance_pct"] / 100)
    stamp = now()
    with st.db:
        st.db.executemany(
            "INSERT INTO source_matches(source_ref, decision, dest_item_id, dest_album_id, dist, method, detail, "
            "decided_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_ref) DO UPDATE SET decision=excluded.decision, "
            "dest_item_id=excluded.dest_item_id, dest_album_id=excluded.dest_album_id, dist=excluded.dist, "
            "method=excluded.method, detail=excluded.detail, decided_at=excluded.decided_at",
            [(ref, d["decision"], d["dest_item_id"], d["dest_album_id"], d["dist"], d["method"], d["detail"], stamp)
             for ref, d in decisions.items()])
    counts = {}
    for d in decisions.values():
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    return dict(sorted(counts.items()))


def effective_decision(decision, reviewed):
    """A person's review answer wins over the automatic decision."""
    return {"same": "same", "different": "new"}.get(reviewed, decision)
