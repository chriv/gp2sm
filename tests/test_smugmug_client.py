import pytest
import requests

from gp2sm.smugmug.client import NotFound, SmugMugClient, SmugMugError, url_name_for


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses):
    session = FakeSession(responses)
    c = SmugMugClient("k", "s", "t", "ts", session_factory=lambda: session, sleep=lambda s: None)
    return c, session


def test_retries_nonce_used_then_succeeds():
    c, s = client([FakeResponse(401, {"Code": 401, "Message": "oauth_problem=nonce_used"}),
                   FakeResponse(200, {"Response": {"ok": 1}})])
    assert c.request("GET", "/x")["Response"]["ok"] == 1
    assert len(s.calls) == 2


def test_retries_network_errors_and_5xx():
    c, s = client([requests.ConnectionError("boom"), FakeResponse(503, {}), FakeResponse(200, {"Response": {}})])
    c.request("GET", "/x")
    assert len(s.calls) == 3


def test_gives_up_after_max_retries():
    c, _ = client([FakeResponse(500, {})] * 6)
    with pytest.raises(SmugMugError):
        c.request("GET", "/x")


def test_404_raises_not_found():
    c, _ = client([FakeResponse(404, {"Code": 404})])
    with pytest.raises(NotFound):
        c.request("GET", "/x")


def test_400_not_retried():
    c, s = client([FakeResponse(400, {"Code": 400, "Message": "bad"})])
    with pytest.raises(SmugMugError) as e:
        c.request("POST", "/x")
    assert e.value.http_status == 400 and len(s.calls) == 1


def test_http_200_stat_fail_raises():
    c, _ = client([FakeResponse(200, {"stat": "fail", "code": 64, "message": "unknown file type"})])
    with pytest.raises(SmugMugError) as e:
        c.request("POST", "/x")
    assert e.value.code == 64


def test_paged_follows_next_page_and_uses_list_key():
    c, s = client([
        FakeResponse(200, {"Response": {"AlbumImage": [{"ImageKey": "a"}],
                                        "Pages": {"NextPage": "/p1?count=1&start=2"}}}),
        FakeResponse(200, {"Response": {"AlbumImage": [{"ImageKey": "b"}], "Pages": {}}}),
    ])
    keys = [i["ImageKey"] for i, _ in c.paged("/p1", "AlbumImage", {"_expand": "ImageMetadata", "count": 1})]
    assert keys == ["a", "b"]
    # SmugMug's NextPage drops _expand: the original params are resent, with the position from NextPage
    assert s.calls[1][1].endswith("/p1")
    assert s.calls[1][2]["params"] == {"_expand": "ImageMetadata", "count": "1", "start": "2"}


def test_album_images_attaches_expanded_metadata():
    md_uri = "/api/v2/image/a-0!metadata"
    c, _ = client([FakeResponse(200, {
        "Response": {"AlbumImage": [{"ImageKey": "a", "Uris": {"ImageMetadata": {"Uri": md_uri}}}]},
        "Expansions": {md_uri: {"ImageMetadata": {"DateTimeCreated": "2020-01-01T00:00:00"}}}})])
    (img, md), = list(c.album_images("K"))
    assert md["DateTimeCreated"] == "2020-01-01T00:00:00"


def test_album_has_image():
    c, _ = client([FakeResponse(200, {"Response": {}}), FakeResponse(404, {})])
    assert c.album_has_image("A", "k") is True
    assert c.album_has_image("A", "k") is False


def test_low_ratelimit_sleeps():
    slept = []
    session = FakeSession([FakeResponse(200, {"Response": {}}, {"x-ratelimit-remaining": "5"})])
    c = SmugMugClient("k", "s", "t", "ts", session_factory=lambda: session, sleep=slept.append)
    c.request("GET", "/x")
    assert slept and c.ratelimit_remaining == 5


def test_url_name_for():
    assert url_name_for("Person iPhone 2026-09") == "Person-iPhone-2026-09"
    assert url_name_for("2026 videos") == "A-2026-videos"
    assert url_name_for("dupes (review)") == "Dupes-review"


def test_write_not_retried_after_504():
    c, s = client([FakeResponse(504, {}), FakeResponse(400, {"Message": "already moved"})])
    with pytest.raises(SmugMugError) as e:
        c.request("POST", "/album/X!moveimages")
    assert e.value.ambiguous and e.value.http_status == 504 and len(s.calls) == 1


def test_write_not_retried_after_network_error():
    c, s = client([requests.ConnectionError("reset")])
    with pytest.raises(SmugMugError) as e:
        c.request("POST", "/x")
    assert e.value.ambiguous and len(s.calls) == 1


def test_write_retried_after_nonce_used_and_429():
    c, s = client([FakeResponse(401, {"Message": "oauth_problem=nonce_used"}), FakeResponse(429, {}),
                   FakeResponse(200, {"Response": {}})])
    c.request("POST", "/x")
    assert len(s.calls) == 3


def test_idempotent_write_is_retried_after_503():
    c, s = client([FakeResponse(503, {}), FakeResponse(200, {"Response": {"Album": {"SortMethod": "Filename"}}})])
    assert c.set_album_sort("K")["SortMethod"] == "Filename"
    assert len(s.calls) == 2


def test_neutral_item_mapping_hides_smugmug_field_names():
    md_uri = "/api/v2/image/abc-0!metadata"
    c, _ = client([FakeResponse(200, {
        "Response": {"AlbumImage": [{"ImageKey": "abc", "Serial": 0, "Uri": "/api/v2/album/AL/image/abc-0",
                                     "FileName": "IMG_1.JPG", "Format": "JPG", "IsVideo": False,
                                     "ArchivedMD5": "m", "ArchivedSize": 10, "OriginalWidth": 4, "OriginalHeight": 3,
                                     "DateTimeUploaded": "2025-01-01", "Uris": {"ImageMetadata": {"Uri": md_uri}}}]},
        "Expansions": {md_uri: {"ImageMetadata": {"DateTimeCreated": "2020-01-02T03:04:05", "Duration": "6.08 s"}}}})])
    (it,) = list(c.list_album_items("AL", with_metadata=True))
    assert (it["item_id"], it["name"], it["md5"], it["size"], it["width"], it["height"]) == ("abc", "IMG_1.JPG", "m", 10, 4, 3)
    assert it["item_ref"] == "/api/v2/album/AL/image/abc-0" and it["is_video"] is False
    assert it["capture_time"] == "2020-01-02T03:04:05" and it["duration_s"] == 6.08
    assert "Uris" not in it["raw"]


def test_item_ref_and_upload_file_result(tmp_path):
    c, s = client([FakeResponse(200, {"stat": "ok", "Image": {"ImageUri": "/api/v2/image/K9-0",
                                                               "AlbumImageUri": "/api/v2/album/AL/image/K9-0"}})])
    assert c.item_ref("AL", "K9") == "/api/v2/album/AL/image/K9-0"
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")  # a closed file: Windows can't reopen a NamedTemporaryFile that's still open
    assert c.upload_file("AL", str(path), "a.jpg", "image/jpeg") == {"item_id": "K9", "item_ref": "/api/v2/album/AL/image/K9-0"}
    assert s.calls[0][2]["headers"]["X-Smug-AlbumUri"] == "/api/v2/album/AL"
