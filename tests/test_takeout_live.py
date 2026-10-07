"""`gp2sm takeout` against real SmugMug, in a private sandbox folder that is deleted afterwards (opt-in).

Run: GP2SM_LIVE_SMUGMUG=path/to/smugmug_config.json python -m pytest -q -m live tests/test_takeout_live.py
Photos only (synthetic videos aren't real video, and SmugMug rejects them; clips are covered by the fake e2e).
"""

import datetime
import io
import os
import sqlite3

import pytest
from PIL import Image

from gp2sm.project import context
from gp2sm.project.init import main as init_main
from gp2sm.takeout import cli
from tests.fixtures.synthetic import make_takeout, pattern_jpeg, png_bytes, sidecar

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("GP2SM_LIVE_SMUGMUG"),
                                 reason="set GP2SM_LIVE_SMUGMUG to a smugmug_config.json")]

T = int(datetime.datetime(2023, 7, 4, 14, 0, tzinfo=datetime.timezone.utc).timestamp())


def heic_bytes(seed):
    pytest.importorskip("pillow_heif")
    img = Image.open(io.BytesIO(pattern_jpeg(seed)))
    exif = img.getexif()
    exif.get_ifd(0x8769)[0x9003] = "2023:07:04 10:00:00"
    exif.get_ifd(0x8769)[0x9011] = "-04:00"
    buf = io.BytesIO()
    img.save(buf, "HEIF", exif=exif)
    return buf.getvalue()


def webp_bytes(seed):
    buf = io.BytesIO()
    Image.open(io.BytesIO(pattern_jpeg(seed))).save(buf, "WEBP")
    return buf.getvalue()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from gp2sm.smugmug.client import SmugMugClient
    client = SmugMugClient.from_config_file(os.environ["GP2SM_LIVE_SMUGMUG"])
    folder_name = "gp2sm-import-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = client.ensure_folder_path(client.root_folder(), folder_name)
    monkeypatch.setattr(context, "client", lambda cfg: client)
    monkeypatch.setenv("GP2SM_CONFIG_DIR", str(tmp_path / "userconf"))
    yield client, folder, folder_name
    for album in client.list_folder_albums(folder_name):
        client.delete_album(album["album_id"])
    client.delete_folder(folder)


def test_import_into_sandbox(sandbox, tmp_path):
    client, folder, folder_name = sandbox
    root = tmp_path / "proj"
    init_main([str(root), "--non-interactive", "--name", "live", "--timezone", "America/New_York",
               "--folder", folder_name], show=lambda *a: None)
    d = "Photos from 2023"
    entries = [
        (f"{d}/IMG_0101.HEIC", heic_bytes(11)), (f"{d}/IMG_0101.HEIC.supplemental-metadata.json", sidecar("IMG_0101.HEIC", T)),
        (f"{d}/IMG_0102.JPG", pattern_jpeg(12)), (f"{d}/IMG_0102.JPG.supplemental-metadata.json", sidecar("IMG_0102.JPG", T + 60)),
        (f"{d}/Screenshot.PNG", png_bytes()), (f"{d}/Screenshot.PNG.supplemental-metadata.json", sidecar("Screenshot.PNG", T + 120)),
        (f"{d}/IMG_0103.jpg", pattern_jpeg(13)), (f"{d}/IMG_0103.jpg.supplemental-metadata.json", sidecar("IMG_0103.jpg", T + 180)),
        (f"{d}/IMG_0103(1).jpg", pattern_jpeg(14)),
        (f"{d}/IMG_0103.jpg.supplemental-metadata(1).json", sidecar("IMG_0103.jpg", T + 181)),
        (f"{d}/pic.webp", webp_bytes(15)), (f"{d}/pic.webp.supplemental-metadata.json", sidecar("pic.webp", T + 240)),
    ]
    make_takeout(root / "takeout" / "takeout-001.zip", entries)

    album = client.ensure_album(folder, "Photos 2023-07")[0]
    (tmp_path / "s.png").write_bytes(png_bytes())
    client.upload_file(album, str(tmp_path / "s.png"), "Screenshot.PNG", "image/png")         # same bytes
    reencoded = io.BytesIO()
    Image.open(io.BytesIO(pattern_jpeg(12))).save(reencoded, "JPEG", quality=55)
    (tmp_path / "r.jpg").write_bytes(reencoded.getvalue())
    client.upload_file(album, str(tmp_path / "r.jpg"), "IMG_0102.JPG", "image/jpeg")        # same picture

    def run(*argv):
        assert cli.main(["--project", str(root), *argv]) == 0

    for step in ("index", "inventory", "dedupe"):
        run(step)
    db = sqlite3.connect(root / "state.db")
    decided = {os.path.basename(r[0].split("::")[1]): r[1] for r in db.execute("SELECT source_ref, decision FROM source_matches")}
    assert decided == {"IMG_0101.HEIC": "new", "IMG_0102.JPG": "same", "Screenshot.PNG": "exact",
                       "IMG_0103.jpg": "new", "IMG_0103(1).jpg": "new", "pic.webp": "new"}
    run("plan")
    run("stage")
    run("upload", "--yes")
    run("verify")
    statuses = dict(db.execute("SELECT upload_name, status FROM uploads"))
    assert statuses == {"IMG_0101.JPG": "done", "IMG_0103.jpg": "done", "IMG_0103(1).jpg": "done", "pic.JPG": "done"}
    assert db.execute("SELECT COUNT(*) FROM uploads WHERE verified_at IS NULL").fetchone()[0] == 0
    on_server = sorted(it["name"] for it in client.list_album_items(album))
    assert on_server == sorted(["Screenshot.PNG", "IMG_0102.JPG", "IMG_0101.JPG", "IMG_0103.jpg",
                                "IMG_0103(1).jpg", "pic.JPG"])
    run("plan")
    assert db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 4      # nothing new to do
