"""Report formats: Markdown (readable), self-contained HTML, JSON, and a per-file CSV."""

import csv
import html
import io
import json

from gp2sm.report.model import OUTCOMES


def _md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _counts(d):
    return ", ".join(f"{k}: {v}" for k, v in d.items()) or "none"


def sections(report):
    """[(title, kind, content)] shared by Markdown and HTML. kind: 'text' | 'table' | 'list'."""
    out = []
    status = (f"In progress: {report['in_progress']} planned steps haven't run yet, so the figures below are "
              "anticipated, not final." if report["in_progress"] else "Final: nothing planned is left to run.")
    out.append(("Status", "text", status))
    acc = report.get("accounting")
    if acc:
        headers = ["file type", "total"] + acc["outcomes"]
        rows = [[r["type"], r["total"]] + [r[o] or "" for o in acc["outcomes"]] for r in acc["rows"]]
        rows.append([acc["totals"]["type"], acc["totals"]["total"]] + [acc["totals"][o] for o in acc["outcomes"]])
        check = ("Every source file is counted exactly once." if acc["ok"] else
                 "WARNING: the counts don't add up: some files are counted twice or missing. Please report this.")
        out.append(("What happened to every file", "table", (headers, rows, check)))
    imp = report.get("import")
    if imp:
        lines = [f"Already there? {_counts(imp['dedupe'])}",
                 f"How that was decided: {_counts(imp['dedupe_methods'])}",
                 f"Decided by you in review: {_counts(imp['reviewed'])}",
                 f"Uploads: {_counts(imp['uploads'])}; verified on the destination: {imp['verified']}, "
                 f"not yet verified: {imp['unverified']}",
                 f"Live Photo clips beside their still, by capture-time gap: {_counts(imp['clip_gaps']) if imp['clip_gaps'] else 'none planned yet'}"]
        if imp["not_planned_notes"]:
            lines.append(f"Not uploaded, and why: {_counts(imp['not_planned_notes'])}")
        lines += [f"Failed: {f['upload_name']} -> {f['target_name']}: {f['error']}" for f in imp["failures"]]
        out.append(("Import evidence", "list", lines))
    org = report.get("organize")
    if org:
        lines = [f"{r['action']}, {r['status']}: {r['n']}" for r in org["plan"]]
        lines += [f"Dated by: {_counts(org['dated_by'])}", f"In undated albums: {org['undated']}",
                  f"Albums in use: {org['albums']} ({org['albums_created']} created by gp2sm)"]
        out.append(("Organize", "list", lines))
    alb = report.get("albums")
    if alb:
        lines = [f"{r['kind']}, {r['status']}: {r['n']}" for r in alb["changes"]] + alb["findings"]
        out.append(("Album names and settings", "list", lines))
    if report.get("targets"):
        out.append(("Albums", "table", (["album", "kind", "planned", "on the destination", "last checked"],
                                        [[t["name"], t["kind"] or "", t["planned"] or "", t["server_count"] or "",
                                          t["checked_at"] or ""] for t in report["targets"]], None)))
    out.append(("Activity", "table", (["command", "started", "finished", "result"],
                                      [[r["command"], r["started"], r["finished"] or "", r["status"] or "running"]
                                       for r in report["runs"]], None)))
    return out


def to_markdown(report):
    parts = [f"# gp2sm report: {report['project'] or 'project'}", "",
             f"Destination folder: {report['folder']}", ""]
    for title, kind, content in sections(report):
        parts += [f"## {title}", ""]
        if kind == "text":
            parts.append(content)
        elif kind == "list":
            parts += [f"- {line}" for line in content]
        else:
            headers, rows, note = content
            parts.append(_md_table(headers, rows))
            if note:
                parts += ["", note]
        parts.append("")
    return "\n".join(parts)


CSS = """:root{--bg:#f6f7f5;--fg:#1d2321;--muted:#5a6561;--line:#d5dcd8;--accent:#2f6f62;--warn:#9b4d0f}
@media (prefers-color-scheme:dark){:root{--bg:#151a19;--fg:#e3e9e6;--muted:#9aa6a2;--line:#2d3836;--accent:#6cc2b0;
--warn:#e8a564}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:72rem;margin:0 auto;
padding:24px 16px}h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.05rem;margin:28px 0 8px;color:var(--accent)}
.muted{color:var(--muted)}.warn{color:var(--warn);font-weight:700}ul{padding-left:1.2rem}
.scroll{overflow-x:auto}table{border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:4px 10px;text-align:left;white-space:nowrap}
th{font-weight:600}tr:last-child td{font-weight:700}"""


def to_html(report):
    e = html.escape
    parts = ["<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>",
             f"<title>gp2sm report: {e(report['project'] or 'project')}</title><style>{CSS}</style></head><body>",
             f"<h1>gp2sm report: {e(report['project'] or 'project')}</h1>",
             f"<p class='muted'>Destination folder: {e(report['folder'])}</p>"]
    for title, kind, content in sections(report):
        parts.append(f"<h2>{e(title)}</h2>")
        if kind == "text":
            parts.append(f"<p>{e(content)}</p>")
        elif kind == "list":
            parts.append("<ul>" + "".join(f"<li>{e(str(x))}</li>" for x in content) + "</ul>")
        else:
            headers, rows, note = content
            parts.append("<div class='scroll'><table><tr>" + "".join(f"<th>{e(str(h))}</th>" for h in headers) + "</tr>")
            parts += ["<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in row) + "</tr>" for row in rows]
            parts.append("</table></div>")
            if note:
                parts.append(f"<p class='{'warn' if note.startswith('WARNING') else 'muted'}'>{e(note)}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def to_json(report):
    return json.dumps(report, indent=1, default=str)


def to_csv(report):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "type", "outcome"])
    for row in report.get("per_file", []):
        w.writerow([row["source"], row["type"], row["outcome"]])
    return buf.getvalue()


__all__ = ["to_markdown", "to_html", "to_json", "to_csv", "OUTCOMES"]
