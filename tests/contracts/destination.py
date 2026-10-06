"""Behavioral contract every PhotoDestination must satisfy.

Reuse it for a new destination plugin: subclass DestinationContract and provide a `dest` fixture that yields
(destination, sandbox_folder_ref), where sandbox_folder_ref is a folder the tests may create albums in and
that the fixture cleans up afterwards.
"""

import uuid

import pytest

from gp2sm.services import PhotoDestination
from gp2sm.smugmug_client import NotFound, SmugMugError
from tests.fixtures.synthetic import jpeg_bytes


def _upload(dest, album_id, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(jpeg_bytes(w=48, h=32, color=(len(name) * 7 % 255, 90, 160)))
    return dest.upload_file(album_id, str(path), name, "image/jpeg")


class DestinationContract:
    @pytest.fixture
    def albums(self, dest):
        d, folder = dest
        tag = uuid.uuid4().hex[:6]
        a = d.ensure_album(folder, f"contract-{tag}-A")
        b = d.ensure_album(folder, f"contract-{tag}-B")
        yield d, a[0], b[0]
        for album_id in (a[0], b[0]):
            try:
                d.delete_album(album_id)
            except NotFound:
                pass

    def test_is_a_destination(self, dest):
        d, _ = dest
        assert isinstance(d, PhotoDestination)

    def test_ensure_album_is_idempotent(self, dest):
        d, folder = dest
        name = f"contract-{uuid.uuid4().hex[:6]}-idem"
        first = d.ensure_album(folder, name)
        second = d.ensure_album(folder, name)
        try:
            assert first[0] == second[0] and first[3] is True and second[3] is False
        finally:
            d.delete_album(first[0])

    def test_upload_listing_count_and_refs(self, albums, tmp_path):
        d, a, _ = albums
        up = _upload(d, a, tmp_path, "contract_1.jpg")
        assert up["item_id"] and up["item_ref"]
        listed = {it["item_id"]: it for it in d.list_album_items(a)}
        assert up["item_id"] in listed and listed[up["item_id"]]["name"] == "contract_1.jpg"
        assert d.album_item_count(a) == 1
        assert d.item_ref(a, up["item_id"]) == listed[up["item_id"]]["item_ref"]
        assert {it["item_id"] for it in d.list_album_items(a, ids_only=True)} == set(listed)

    def test_move_is_reflected_everywhere(self, albums, tmp_path):
        d, a, b = albums
        up = _upload(d, a, tmp_path, "contract_2.jpg")
        d.move_items(b, [d.item_ref(a, up["item_id"])])
        assert d.album_contains(b, up["item_id"]) and not d.album_contains(a, up["item_id"])
        assert d.item_album_ids(up["item_id"]) == [b]
        assert (d.album_item_count(a), d.album_item_count(b)) == (0, 1)

    def test_batch_with_bad_ref_moves_nothing_when_atomic(self, albums, tmp_path):
        d, a, b = albums
        if not d.capabilities.atomic_batch_moves:
            pytest.skip("destination doesn't declare atomic batch moves")
        good = _upload(d, a, tmp_path, "contract_3.jpg")
        bogus = d.item_ref(a, "ZZZZZZZ")
        with pytest.raises(SmugMugError) as e:
            d.move_items(b, [d.item_ref(a, good["item_id"]), bogus])
        assert e.value.http_status == 400 and not e.value.ambiguous
        assert d.album_contains(a, good["item_id"]) and d.album_item_count(b) == 0

    def test_remove_then_remove_again_is_not_found(self, albums, tmp_path):
        d, a, _ = albums
        up = _upload(d, a, tmp_path, "contract_4.jpg")
        d.remove_item(d.item_ref(a, up["item_id"]))
        assert d.album_item_count(a) == 0
        with pytest.raises(NotFound):
            d.remove_item(d.item_ref(a, up["item_id"]))
