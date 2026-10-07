"""`gp2sm takeout` end to end on a synthetic Takeout, against the fake destination.

Covers: .zip and .tgz, a Live Photo pair split across archives, '(N)' collision names, an item already on the
destination byte for byte, one already there as a re-encoded copy (content match), burst frames sharing a
name, a clip with no still, idempotent re-planning, and an interrupted upload resumed.
"""

import io
import os
import sqlite3

import pytest
from PIL import Image

from gp2sm.project import context
from gp2sm.project.init import main as init_main
from gp2sm.state import State
from gp2sm.takeout import cli
from tests.fakes.smugmug import FakeSmugMug
from tests.fixtures.synthetic import jpeg_bytes, make_takeout, mp4_bytes, pattern_jpeg, standard_takeout_entries


def run(project, *argv):
    return cli.main(["--project", str(project), *argv])


@pytest.fixture(params=["tgz", "zip"])
def project(tmp_path, monkeypatch, request):
    fmt = request.param
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    root = tmp_path / "proj"
    init_main([str(root), "--non-interactive", "--name", "T", "--timezone", "UTC", "--folder", "Photos"],
              show=lambda *a: None)
    entries = standard_takeout_entries()
    replace = {"IMG_0001.MP4": mp4_bytes(created_unix=1_688_479_200, pad=70_000),   # big enough to be accepted
               "IMG_0999.MP4": mp4_bytes(created_unix=1_688_479_700, pad=70_000),
               "lp_image.heic": pattern_jpeg(1), "lp_image(1).heic": pattern_jpeg(2)}  # burst frames, distinct
    entries = [(p, replace.get(os.path.basename(p), d)) for p, d in entries]
    clip = [e for e in entries if e[0].endswith("IMG_0001.MP4")]
    rest = [e for e in entries if not e[0].endswith("IMG_0001.MP4")]
    make_takeout(root / "takeout" / f"takeout-001.{fmt}", rest)
    make_takeout(root / "takeout" / f"takeout-002.{fmt}", clip)      # Live Photo pair split across archives

    fake = FakeSmugMug(min_video_bytes=60_000)
    album = fake.ensure_album(fake.ensure_folder_path(fake.root_folder(), "Photos"), "Photos 2023-07")[0]
    screenshot = dict(entries)["Photos from 2023/Screenshot.PNG"]
    (tmp_path / "s.png").write_bytes(screenshot)
    fake.upload_file(album, str(tmp_path / "s.png"), "Screenshot.PNG", "image/png")      # byte-identical copy
    lp = dict(entries)["Photos from 2023/lp_image.heic"]
    reencoded = io.BytesIO()
    Image.open(io.BytesIO(lp)).save(reencoded, "JPEG", quality=60)   # same picture, different bytes
    (tmp_path / "lp.jpg").write_bytes(reencoded.getvalue())
    up = fake.upload_file(album, str(tmp_path / "lp.jpg"), "lp_image.JPG", "image/jpeg")  # re-encoded copy
    fake.items[up["item_id"]]["preview"] = lp
    monkeypatch.setattr(context, "client", lambda cfg: fake)
    return root, fake, album


def uploads(root):
    db = sqlite3.connect(root / "state.db")
    db.row_factory = sqlite3.Row
    return {r["upload_name"]: dict(r) for r in db.execute("SELECT * FROM uploads")}


def test_import_end_to_end(project, capsys):
    root, fake, album = project
    assert run(root, "index") == 0
    assert run(root, "inventory") == 0
    assert run(root, "dedupe") == 0
    st = State(str(root / "state.db"))
    decided = {os.path.basename(r[0].split("::")[1]): r[1] for r in st.q("SELECT source_ref, decision FROM source_matches")}
    assert decided == {"Screenshot.PNG": "exact", "lp_image.heic": "same", "lp_image(1).heic": "new",
                       "IMG_0001.HEIC": "new", "IMG_0999.MP4": "new"}

    assert run(root, "plan") == 0
    planned = uploads(root)
    assert set(planned) == {"IMG_0001.JPG", "IMG_0001.MP4", "lp_image(1).JPG", "IMG_0999.MP4"}
    assert planned["IMG_0001.MP4"]["target_name"] == planned["IMG_0001.JPG"]["target_name"] == "Photos 2023-07"
    assert planned["IMG_0001.MP4"]["archive"].startswith("takeout-002")   # the clip's own archive
    assert planned["IMG_0999.MP4"]["reason"] == "clip with no matching still"
    assert run(root, "plan") == 0 and uploads(root) == planned          # re-planning adds nothing

    assert run(root, "stage") == 0
    assert run(root, "upload") == 0                                       # dry run
    assert not any(v["status"] == "done" for v in uploads(root).values())
    assert "Dry run" in capsys.readouterr().out
    assert run(root, "upload", "--yes", "--limit", "1") == 0              # interrupted after one file ...
    assert run(root, "upload", "--yes") == 0                               # ... and resumed
    assert run(root, "verify") == 0
    done = uploads(root)
    assert {v["status"] for v in done.values()} == {"done"} and all(v["verified_at"] for v in done.values())
    names_on_dest = sorted(fake.items[i]["name"] for i in fake.albums[album])
    assert names_on_dest == sorted(["Screenshot.PNG", "lp_image.JPG", "IMG_0001.JPG", "IMG_0001.MP4",
                                    "lp_image(1).JPG", "IMG_0999.MP4"])
    assert not os.path.exists(root / ".gp2sm.lock")


def test_review_round_trip(project, monkeypatch):
    root, fake, album = project
    for step in ("index", "inventory"):
        run(root, step)
    monkeypatch.setitem(fake.items[next(i for i in fake.albums[album] if fake.items[i]["name"] == "lp_image.JPG")],
                        "preview", jpeg_bytes(w=64, h=48, color=(250, 250, 250)))   # now unclear vs. the source
    from gp2sm.importer import dedupe
    monkeypatch.setattr(dedupe, "decide", _force_review(dedupe.decide, "lp_image.heic"))
    run(root, "dedupe")
    run(root, "plan")
    held = [v for v in uploads(root).values() if v["source_ref"].endswith("/lp_image.heic")]
    assert [(v["upload_name"], v["status"]) for v in held] == [("lp_image (1).JPG", "needs_review")]
    assert run(root, "review") == 0
    files = sorted(os.listdir(root / "review"))
    pair = [f for f in files if f.startswith("0001 ")]
    assert pair == ["0001 destination lp_image.JPG", "0001 source lp_image.jpg"]
    for f in pair:
        os.rename(root / "review" / f, root / "review" / "different" / f)
    assert run(root, "review", "--read") == 0
    run(root, "plan")
    assert uploads(root)["lp_image (1).JPG"]["status"] == "planned"       # a person said: different, so upload


def _force_review(decide, name):
    def wrapped(*a, **kw):
        out = decide(*a, **kw)
        for ref, d in out.items():
            if ref.endswith("/" + name):
                d.update(decision="review", detail="forced for the test")
        return out
    return wrapped
