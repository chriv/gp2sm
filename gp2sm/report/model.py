"""The project report's data: gathered from the state database (and the Takeout index), with no formatting.

The centre of it is the accounting: every media file in the source (photos, videos, Live Photo clips, files
without metadata) gets exactly one outcome, per file type, and the rows must add up to the total. `build()`
checks that and says so in `accounting_ok`; a report never hides a file.
"""

import collections
import os
import re

# Outcomes, in the order a reader expects them. Each source file gets exactly one.
OUTCOMES = [
    "already there (same file)",
    "already there (same picture)",
    "clip already beside its still",
    "uploaded into a dated album",
    "uploaded into an undated album",
    "not uploaded yet",
    "held for review",
    "duplicate within the source",
    "skipped by a policy",
    "upload failed",
    "not planned",
]
PENDING_UPLOAD = ("planned", "staged", "uploading", "unknown")


def _ext(ref):
    name = ref.split("::", 1)[-1].rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    return ext or "(none)"


def classify_sources(sources, uploads, matches, undated_albums):
    """{source ref: (file type label, outcome)}.

    sources: [dict(ref, kind ('photo'|'video'|'clip'), still_ref (clips), policy_skip: None, or the policy that
              skips it: 'rejected_types' (photos), 'live_clips' or 'unpaired_clips' (clips))]
    uploads: {(ref, role) or ref: dict(status, target_name)} keyed by source ref (any role)
    matches: {ref: effective dedupe decision}
    """
    out = {}
    for s in sources:
        ref, kind = s["ref"], s["kind"]
        label = _ext(ref) + (" Live Photo clip" if kind == "clip" else " video" if kind == "video" else "")
        up = uploads.get(ref)
        decision = matches.get(s.get("still_ref") or ref) if kind == "clip" else matches.get(ref)
        if up:
            status = up["status"]
            if status == "done":
                outcome = ("uploaded into an undated album" if up["target_name"] in undated_albums
                           else "uploaded into a dated album")
            elif status in PENDING_UPLOAD:
                outcome = "not uploaded yet"
            elif status in ("needs_review", "held"):
                outcome = "held for review"
            elif status == "skipped":
                outcome = "skipped by a policy"
            else:
                outcome = "upload failed"
        elif kind == "clip":
            skip = s.get("policy_skip")
            if skip == "live_clips":
                outcome = "skipped by a policy"
            elif decision in ("exact", "same"):
                outcome = "clip already beside its still"
            elif decision == "review":
                outcome = "held for review"
            elif skip:   # unpaired_clips = "skip": a clip whose still isn't on the destination wasn't paired
                outcome = "skipped by a policy"
            else:
                outcome = "not planned"
        elif decision == "exact":
            outcome = "already there (same file)"
        elif decision == "same":
            outcome = "already there (same picture)"
        elif decision == "source_duplicate":
            outcome = "duplicate within the source"
        elif decision == "review":
            outcome = "held for review"
        elif s.get("policy_skip"):
            outcome = "skipped by a policy"
        else:
            outcome = "not planned"
        out[ref] = (label, outcome)
    return out


def accounting(classified):
    """Rows per file type with a count per outcome, totals, and whether every file is counted exactly once."""
    table = collections.defaultdict(collections.Counter)
    for label, outcome in classified.values():
        table[label][outcome] += 1
    rows = [{"type": label, "total": sum(c.values()), **{o: c.get(o, 0) for o in OUTCOMES}}
            for label, c in sorted(table.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))]
    totals = {"type": "all files", "total": sum(r["total"] for r in rows),
              **{o: sum(r[o] for r in rows) for o in OUTCOMES}}
    ok = totals["total"] == len(classified) == sum(totals[o] for o in OUTCOMES)
    used = [o for o in OUTCOMES if totals[o]]
    return {"rows": rows, "totals": totals, "outcomes": used, "ok": ok}


GAP = re.compile(r"Live Photo clip, (\d+)s from its still")


def clip_gaps(reasons):
    """Histogram of clip-to-still capture-time gaps from upload reasons: {'0 s': n, '1 s': n, '2-5 s': n, ...}."""
    buckets = collections.Counter()
    for reason in reasons:
        m = GAP.search(reason or "")
        if not m:
            continue
        g = int(m.group(1))
        buckets["0 s" if g == 0 else "1 s" if g == 1 else "2-5 s" if g <= 5 else "6-30 s" if g <= 30 else "31-60 s"] += 1
    order = ["0 s", "1 s", "2-5 s", "6-30 s", "31-60 s"]
    return {k: buckets[k] for k in order if buckets[k]}


def build(st, cfg, sources=None, policy_notes=None):
    """The whole report as plain data. sources: the Takeout classification input (None if the project has no
    Takeout); policy_notes: the plan's notes on what it didn't plan and why (from the last takeout plan run)."""
    q = st.q
    undated = {cfg["undated_photo_album"], cfg["undated_video_album"]}
    report = {"project": cfg.get("project_name") or "", "folder": cfg["target_folder"], "sections": []}

    pending = (st.one("SELECT COUNT(*) FROM uploads WHERE status IN ('planned','staged','uploading','unknown')") or 0) \
        + (st.one("SELECT COUNT(*) FROM plan WHERE status IN ('pending','in_progress','unknown')") or 0) \
        + (st.one("SELECT COUNT(*) FROM album_changes WHERE status='planned'") or 0)
    report["in_progress"] = pending

    if sources is not None:
        uploads = {r["source_ref"]: dict(r) for r in q("SELECT source_ref, role, status, target_name, verified_at "
                                                       "FROM uploads WHERE source_ref IS NOT NULL")}
        matches = {r["source_ref"]: ({"same": "same", "different": "new"}.get(r["reviewed"], r["decision"]))
                   for r in q("SELECT source_ref, decision, reviewed FROM source_matches")}
        classified = classify_sources(sources, uploads, matches, undated)
        report["accounting"] = accounting(classified)
        report["per_file"] = [{"source": ref, "type": t, "outcome": o} for ref, (t, o) in sorted(classified.items())]
        report["import"] = {
            "dedupe": dict(q("SELECT decision, COUNT(*) FROM source_matches GROUP BY 1 ORDER BY 2 DESC")),
            "dedupe_methods": dict(q("SELECT method, COUNT(*) FROM source_matches GROUP BY 1 ORDER BY 2 DESC")),
            "reviewed": dict(q("SELECT reviewed, COUNT(*) FROM source_matches WHERE reviewed IS NOT NULL GROUP BY 1")),
            "uploads": dict(q("SELECT status, COUNT(*) FROM uploads GROUP BY 1 ORDER BY 2 DESC")),
            "verified": st.one("SELECT COUNT(*) FROM uploads WHERE status='done' AND verified_at IS NOT NULL") or 0,
            "unverified": st.one("SELECT COUNT(*) FROM uploads WHERE status='done' AND verified_at IS NULL") or 0,
            "clip_gaps": clip_gaps(r[0] for r in q("SELECT reason FROM uploads WHERE role='clip'")),
            "not_planned_notes": policy_notes or {},
            "failures": [dict(r) for r in q("SELECT upload_name, target_name, substr(last_error, 1, 120) AS error "
                                            "FROM uploads WHERE status='failed' LIMIT 20")],
        }
    if st.one("SELECT COUNT(*) FROM plan"):
        methods = collections.Counter()
        for (reason,) in q("SELECT reason FROM plan WHERE action IN ('move', 'collect')"):
            m = re.match(r"date via ([a-z_]+)", reason or "")
            methods[m.group(1) if m else "other"] += 1
        report["organize"] = {
            "plan": [dict(r) for r in q("SELECT action, status, COUNT(*) AS n FROM plan GROUP BY 1, 2 ORDER BY 1, 2")],
            "dated_by": dict(methods.most_common()),
            "undated": st.one("SELECT COUNT(*) FROM plan p JOIN targets t ON t.name=p.target_name "
                              "WHERE t.kind IN ('photo_undated', 'video_undated')") or 0,
            "albums": st.one("SELECT COUNT(*) FROM targets WHERE album_id IS NOT NULL") or 0,
            "albums_created": st.one("SELECT COUNT(*) FROM targets WHERE created_at IS NOT NULL") or 0,
        }
    if st.one("SELECT COUNT(*) FROM album_changes"):
        report["albums"] = {
            "changes": [dict(r) for r in q("SELECT kind, status, COUNT(*) AS n FROM album_changes "
                                           "GROUP BY 1, 2 ORDER BY 1, 2")],
            "findings": [r[0] for r in q("SELECT note FROM album_changes WHERE kind='finding' LIMIT 30")],
        }
    report["runs"] = [dict(r) for r in q("SELECT command, started, finished, status FROM runs "
                                         "ORDER BY run_id DESC LIMIT 30")]
    report["targets"] = [dict(r) for r in q("SELECT name, kind, planned, server_count, checked_at FROM targets "
                                            "ORDER BY name")]
    return report
