from gp2sm.services import Capabilities, PhotoDestination, SourceItem
from gp2sm.smugmug_client import SMUGMUG_CAPABILITIES, SmugMugClient
from tests.fakes.smugmug import FakeSmugMug


def test_smugmug_client_implements_destination():
    client = SmugMugClient("k", "s", "t", "ts", session_factory=lambda: None)
    assert isinstance(client, PhotoDestination)
    assert isinstance(client.capabilities, Capabilities)


def test_fake_implements_destination_with_same_capabilities():
    fake = FakeSmugMug()
    assert isinstance(fake, PhotoDestination)
    assert fake.capabilities is SMUGMUG_CAPABILITIES


def test_smugmug_capabilities_match_verified_behavior():
    caps = SMUGMUG_CAPABILITIES
    assert caps.atomic_batch_moves and caps.moves_ignored_while_processing and not caps.can_rename_items
    assert ".jpg" in caps.stores_original_bytes and ".heic" not in caps.stores_original_bytes
    assert dict(caps.converts_on_upload)[".heic"] == ".jpg"
    assert caps.max_items_per_album == 5000


def test_source_item_is_immutable_record():
    item = SourceItem(source_id="x", name="IMG_1.HEIC", kind="photo", taken_ts=1_700_000_000)
    assert item.extras == {} and item.motion_ref is None
