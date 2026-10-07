"""Pure organize planning: each item's destination album from its date and group (no I/O, unit tested)."""

from collections import defaultdict


def part_name(base, n):
    return base if n == 1 else f"{base} - Part {n}"


# ------------------------------------------------------------- organize (rules)

def plan_organize(items, settings, existing=None):
    """Targets for items already dated and grouped by organize.rules (pure).

    items: [dict(item_id, filename, is_video, md5, uploaded, capture_local, method, group)]
    settings: dict(photo, video, undated_photo, undated_video (templates; {yyyy} {mm} {group}),
                   duplicates_album, duplicates ('park' | 'keep'), soft_cap, mode ('move' | 'collect'))
    existing: {item_id: dict(status, target_name)}; items whose move is done keep their album.
    Byte-identical copies: with 'park', one keeper per MD5 (earliest upload) is organized and the others go to
    the duplicates album; any copy's date can date the keeper. Albums over soft_cap continue as '- Part N'.
    mode 'collect' adds items to their album and leaves them in the source (action 'collect'); nothing is
    parked then, since moving items out of a source an uploader app still writes to may make it upload again.
    Returns [dict(item_id, action, target_name, kind, reason)].
    """
    existing = existing or {}
    collect = settings.get("mode") == "collect"
    groups = {}
    if settings["duplicates"] == "park" and not collect:
        for it in items:
            if it.get("md5"):
                groups.setdefault(it["md5"], []).append(it)
    keeper_of, dated_by = {}, {}
    for members in groups.values():
        keeper = min(members, key=lambda it: (it.get("uploaded") or "", it["item_id"]))
        dated = next((m for m in sorted(members, key=lambda m: m is not keeper) if m.get("capture_local")), None)
        for m in members:
            keeper_of[m["item_id"]] = keeper["item_id"]
        if dated:
            dated_by[keeper["item_id"]] = dated

    def fill(template, capture_local, group):
        yyyy, mm = (capture_local[:4], capture_local[5:7]) if capture_local else ("", "")
        return " ".join(template.format(yyyy=yyyy, mm=mm, group=group or "").split())

    rows = []
    for it in items:
        keeper = keeper_of.get(it["item_id"], it["item_id"])
        if keeper != it["item_id"]:
            rows.append({"item_id": it["item_id"], "action": "park_duplicate", "base": settings["duplicates_album"],
                         "kind": "duplicates", "sort": (it["md5"], it["item_id"]),
                         "reason": f"same file as {keeper}"})
            continue
        src = dated_by.get(it["item_id"], it)
        when, method = src.get("capture_local"), src.get("method") or "none"
        if src is not it:
            method = f"{method} (from an identical copy)"
        video = bool(it.get("is_video"))
        if when:
            base, kind = fill(settings["video" if video else "photo"], when, it.get("group")), "video" if video else "photo"
        else:
            base = fill(settings["undated_video" if video else "undated_photo"], None, it.get("group"))
            kind = "video_undated" if video else "photo_undated"
        group = f", group {it['group']}" if it.get("group") else ""
        rows.append({"item_id": it["item_id"], "action": "collect" if collect else "move", "base": base, "kind": kind,
                     "sort": (when or "", it["item_id"]), "reason": f"date via {method}{group}"})

    by_base = defaultdict(list)
    for r in rows:
        by_base[r["base"]].append(r)
    for base, members in by_base.items():
        members.sort(key=lambda r: r["sort"])
        for i, r in enumerate(members):
            prior = existing.get(r["item_id"])
            if prior and prior["status"] == "done":
                r["target_name"] = prior["target_name"]
            else:
                r["target_name"] = part_name(base, i // settings["soft_cap"] + 1)
    return [{k: r[k] for k in ("item_id", "action", "target_name", "kind", "reason")} for r in rows]
