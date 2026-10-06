import pytest
import requests

from gp2sm.smugmug_client import NotFound, SmugMugClient, SmugMugError, url_name_for


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
        FakeResponse(200, {"Response": {"AlbumImage": [{"ImageKey": "a"}], "Pages": {"NextPage": "/p2"}}}),
        FakeResponse(200, {"Response": {"AlbumImage": [{"ImageKey": "b"}], "Pages": {}}}),
    ])
    keys = [i["ImageKey"] for i, _ in c.paged("/p1", "AlbumImage")]
    assert keys == ["a", "b"]
    assert s.calls[1][2]["params"] is None  # NextPage already carries the query


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
