"""Behavioral contract every PhotoSource must satisfy.

Subclass SourceContract and provide a `source` fixture yielding a PhotoSource with at least one photo
(ideally also a Live Photo pair).
"""

import hashlib

from gp2sm.services import PhotoSource, SourceItem


class SourceContract:
    def test_is_a_source(self, source):
        assert isinstance(source, PhotoSource) and source.name

    def test_items_are_well_formed_unique_and_stable(self, source):
        first = list(source.iter_items())
        assert first and all(isinstance(i, SourceItem) for i in first)
        assert len({i.source_id for i in first}) == len(first)
        assert all(i.kind in ("photo", "video") for i in first)
        assert all(i.taken_ts is None or isinstance(i.taken_ts, int) for i in first)
        assert [i.source_id for i in source.iter_items()] == [i.source_id for i in first]  # deterministic order

    def test_bytes_match_declared_size_and_md5(self, source):
        for item in source.iter_items():
            data = source.open_bytes(item)
            if item.size is not None:
                assert len(data) == item.size
            if item.md5:
                assert hashlib.md5(data).hexdigest() == item.md5
