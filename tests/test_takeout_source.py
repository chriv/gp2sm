import pytest

from gp2sm.takeout import index as takeout_index
from gp2sm.takeout.source import TakeoutSource
from tests.contracts.source import SourceContract
from tests.fixtures.synthetic import make_takeout, standard_takeout_entries


@pytest.fixture
def takeout(tmp_path):
    entries = standard_takeout_entries()
    clip = [e for e in entries if e[0].endswith("IMG_0001.MP4")]
    rest = [e for e in entries if not e[0].endswith("IMG_0001.MP4")]
    make_takeout(tmp_path / "takeout-1.tgz", rest)
    make_takeout(tmp_path / "takeout-2.tgz", clip)   # Live Photo pair split across archives
    db = tmp_path / "idx.db"
    takeout_index.main([str(tmp_path / "takeout-1.tgz"), str(tmp_path / "takeout-2.tgz"), "--db", str(db)])
    return TakeoutSource(str(db), str(tmp_path))


class TestTakeoutSourceContract(SourceContract):
    @pytest.fixture
    def source(self, takeout):
        return takeout


def test_items_and_live_pair(takeout):
    items = {i.name: i for i in takeout.iter_items()}
    assert set(items) == {"IMG_0001.HEIC", "lp_image.heic", "lp_image(1).heic", "Screenshot.PNG"}
    still = items["IMG_0001.HEIC"]
    assert still.kind == "photo" and still.taken_ts and still.motion_ref.startswith("takeout-2.tgz::")
    assert takeout.open_ref(still.motion_ref)[:12].endswith(b"ftypmp42")
    # '(N)' sidecar paired to the right file, with its own capture time
    assert items["lp_image(1).heic"].taken_ts == items["lp_image.heic"].taken_ts + 1


def test_orphans_optional(takeout):
    names = {i.name for i in takeout.iter_items(include_orphans=True)}
    assert "IMG_0999.MP4" in names and "IMG_0999.MP4" not in {i.name for i in takeout.iter_items()}


def test_iter_bytes_single_pass_covers_everything(takeout):
    items = list(takeout.iter_items())
    got = {item.source_id: data for item, data in takeout.iter_bytes(items)}
    assert set(got) == {i.source_id for i in items}
