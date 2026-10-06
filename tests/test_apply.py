import pytest

from gp2sm import consolidate
from gp2sm.smugmug_client import NotFound, SmugMugError
from gp2sm.state import State


class FakeSmug:
    """In-memory albums: {album_key: set(image_key)}. Mirrors verified SmugMug semantics."""

    def __init__(self, albums, bad=(), fail_network=0):
        self.albums = {k: set(v) for k, v in albums.items()}
        self.bad = set(bad)
        self.fail_network = fail_network

    def album(self, key):
        return {"ImageCount": len(self.albums[key])}

    def move_images(self, dest, uris):
        moves = []
        for uri in uris:
            parts = uri.split("/")
            src, image_key = parts[4], parts[6].split("-")[0]
            if image_key in self.bad or image_key not in self.albums.get(src, ()):
                raise SmugMugError("bad uri", http_status=400)  # all-or-nothing
            moves.append((src, image_key))
        if self.fail_network:
            self.fail_network -= 1
            for src, k in moves:  # the move happened, but the client never heard back
                self.albums[src].discard(k)
                self.albums[dest].add(k)
            raise SmugMugError("timeout", http_status=None)
        for src, k in moves:
            self.albums[src].discard(k)
            self.albums[dest].add(k)

    def album_has_image(self, album_key, image_key, serial=0):
        return image_key in self.albums[album_key]

    def image_album_keys(self, image_key, serial=0):
        found = [a for a, keys in self.albums.items() if image_key in keys]
        if not found:
            raise NotFound("gone", http_status=404)
        return found


@pytest.fixture
def st(tmp_path):
    s = State(str(tmp_path / "s.db"))
    s.start_run("test", {})
    s.db.execute("INSERT INTO targets(name, kind, album_key) VALUES('T', 'photo', 'DST')")
    for k in "abcd":
        s.db.execute("INSERT INTO images(image_key, serial, src_album_key, current_album_key) VALUES(?,0,'SRC','SRC')",
                     (k,))
        s.db.execute("INSERT INTO plan(image_key, action, target_name, status) VALUES(?,'move','T','pending')", (k,))
    s.db.commit()
    return s


def rows(st):
    return [dict(r) for r in st.q("SELECT p.image_key, i.serial, i.current_album_key FROM plan p "
                                  "JOIN images i USING(image_key) ORDER BY 1")]


def status(st):
    return {r[0]: r[1] for r in st.q("SELECT image_key, status FROM plan")}


def test_clean_batch_moves_and_records(st):
    fake = FakeSmug({"SRC": "abcd", "DST": ""})
    done, failed = consolidate.move_batch(st, fake, "DST", rows(st), "b1")
    assert sorted(done) == list("abcd") and failed == []
    assert set(status(st).values()) == {"done"}
    assert {r["current_album_key"] for r in rows(st)} == {"DST"}
    assert st.one("SELECT COUNT(*) FROM events WHERE kind='batch_moved'") == 1


def test_bad_item_is_isolated_and_rest_still_move(st):
    fake = FakeSmug({"SRC": "abcd", "DST": ""}, bad={"c"})
    done, failed = consolidate.move_batch(st, fake, "DST", rows(st), "b1")
    assert sorted(done) == list("abd") and failed == ["c"]
    assert status(st)["c"] == "failed"
    assert fake.albums["DST"] == set("abd")


def test_unknown_outcome_is_reconciled_from_server(st):
    fake = FakeSmug({"SRC": "abcd", "DST": ""}, fail_network=1)
    done, failed = consolidate.move_batch(st, fake, "DST", rows(st), "b1")
    assert sorted(done) == list("abcd") and failed == []
    assert set(status(st).values()) == {"done"}


def test_reconcile_sets_pending_when_still_in_source(st):
    fake = FakeSmug({"SRC": "abcd", "DST": ""})
    st.set_plan_status(["a"], "in_progress")
    st.db.commit()
    assert consolidate.reconcile_keys(st, fake, ["a"]) == {"done": 0, "pending": 1, "failed": 0}
    assert status(st)["a"] == "pending"


class TimeoutAfterApplying(FakeSmug):
    """Server applies the move but the client sees a 504 (what happened in the 2023-07 batch)."""

    def move_images(self, dest, uris):
        FakeSmug.move_images(self, dest, uris)
        raise SmugMugError("504", http_status=504, ambiguous=True)


def test_ambiguous_504_is_reconciled_not_failed(st):
    fake = TimeoutAfterApplying({"SRC": "abcd", "DST": ""})
    done, failed = consolidate.move_batch(st, fake, "DST", rows(st), "b1")
    assert sorted(done) == list("abcd") and failed == []


def test_single_400_already_in_target_counts_as_done(st):
    fake = FakeSmug({"SRC": "bcd", "DST": "a"})  # 'a' already moved by an earlier, unrecorded request
    a = [r for r in rows(st) if r["image_key"] == "a"]
    done, failed = consolidate.move_batch(st, fake, "DST", a, "b1")
    assert done == ["a"] and failed == []
