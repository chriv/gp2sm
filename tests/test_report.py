"""The project report: per-file accounting (every file exactly once), formats, and the gp2sm report command."""

import json
import os

from gp2sm.report import model, render
from tests.test_takeout_import_e2e import project  # noqa: F401  (fixture reused)
from tests.test_takeout_import_e2e import run as takeout

UNDATED = {"Photos Undated", "Videos Undated"}


def test_classify_gives_each_file_one_outcome():
    sources = [{"ref": "a::P/IMG_1.HEIC", "kind": "photo"}, {"ref": "a::P/IMG_1.MP4", "kind": "clip", "still_ref": "a::P/IMG_1.HEIC"},
               {"ref": "a::P/IMG_2.HEIC", "kind": "photo"}, {"ref": "a::P/IMG_2.MP4", "kind": "clip", "still_ref": "a::P/IMG_2.HEIC"},
               {"ref": "a::P/x.jpg", "kind": "photo"}, {"ref": "a::P/y.jpg", "kind": "photo"},
               {"ref": "a::P/pic.webp", "kind": "photo", "policy_skip": "rejected_types"},
               {"ref": "a::P/IMG_3.HEIC", "kind": "photo"}, {"ref": "a::P/vid.MOV", "kind": "video"},
               {"ref": "a::P/IMG_4.HEIC", "kind": "photo"}, {"ref": "a::P/IMG_4.MP4", "kind": "clip", "still_ref": "a::P/IMG_4.HEIC",
                                                              "policy_skip": "unpaired_clips"}]
    uploads = {"a::P/IMG_1.HEIC": {"status": "done", "target_name": "Photos 2023-07"},
               "a::P/IMG_1.MP4": {"status": "done", "target_name": "Photos 2023-07"},
               "a::P/IMG_3.HEIC": {"status": "needs_review", "target_name": "Photos 2023-07"},
               "a::P/vid.MOV": {"status": "staged", "target_name": "Videos Undated"}}
    matches = {"a::P/IMG_2.HEIC": "same", "a::P/x.jpg": "exact", "a::P/y.jpg": "source_duplicate",
               "a::P/IMG_4.HEIC": "new"}
    c = model.classify_sources(sources, uploads, matches, UNDATED)
    assert {ref.split("/")[-1]: outcome for ref, (_, outcome) in c.items()} == {
        "IMG_1.HEIC": "uploaded into a dated album", "IMG_1.MP4": "uploaded into a dated album",
        "IMG_2.HEIC": "already there (same picture)", "IMG_2.MP4": "clip already beside its still",
        "x.jpg": "already there (same file)", "y.jpg": "duplicate within the source",
        "pic.webp": "skipped by a policy", "IMG_3.HEIC": "held for review", "vid.MOV": "not uploaded yet",
        "IMG_4.HEIC": "not planned", "IMG_4.MP4": "skipped by a policy"}
    acc = model.accounting(c)
    assert acc["ok"] and acc["totals"]["total"] == 11 and sum(acc["totals"][o] for o in acc["outcomes"]) == 11
    assert {r["type"] for r in acc["rows"]} >= {".heic", ".mp4 Live Photo clip", ".mov video", ".webp"}


def test_clip_gap_histogram():
    reasons = ["Live Photo clip, 0s from its still", "Live Photo clip, 1s from its still (paired by time)",
               "Live Photo clip, 4s from its still", "new photo"]
    assert model.clip_gaps(reasons) == {"0 s": 1, "1 s": 1, "2-5 s": 1}


def test_report_command_end_to_end(project):     # noqa: F811
    root, fake, album = project
    for step in ("index", "inventory", "dedupe", "plan"):
        assert takeout(root, step) == 0
    from gp2sm.report import cli
    assert cli.main(["--project", str(root)]) == 0
    md = (root / "reports" / "report.md").read_text()
    assert "In progress" in md and "Every source file is counted exactly once." in md
    data = json.loads((root / "reports" / "report.json").read_text())
    assert data["accounting"]["ok"] and data["accounting"]["totals"]["total"] == 6
    outcomes = {r["source"].rsplit("/", 1)[-1]: r["outcome"] for r in data["per_file"]}
    assert outcomes["Screenshot.PNG"] == "already there (same file)"
    assert outcomes["lp_image.heic"] == "already there (same picture)"
    assert outcomes["IMG_0001.MP4"] == "not uploaded yet"
    assert os.path.exists(root / "reports" / "report.html") and (root / "reports" / "files.csv").read_text().count("\n") == 7
    for step in ("stage",):
        takeout(root, step)
    takeout(root, "upload", "--yes")
    takeout(root, "verify")
    assert cli.main(["--project", str(root)]) == 0
    md = (root / "reports" / "report.md").read_text()
    assert "Final: nothing planned is left to run." in md
    data = json.loads((root / "reports" / "report.json").read_text())
    assert data["import"]["verified"] == 4 and data["import"]["unverified"] == 0
    assert set(data["accounting"]["outcomes"]) == {"already there (same file)", "already there (same picture)",
                                                   "uploaded into a dated album"}


def test_renderers_escape_and_mark_problems():
    report = {"project": "<P>", "folder": "F", "in_progress": 0, "runs": [], "targets": [],
              "accounting": {"rows": [], "outcomes": [], "ok": False,
                             "totals": {"type": "all files", "total": 1}}}
    assert "gp2sm report: &lt;P&gt;" in render.to_html(report) and "WARNING" in render.to_html(report)
    assert "WARNING: the counts don't add up" in render.to_markdown(report)
