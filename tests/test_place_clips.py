from gp2sm.media import aspect, mp4_dims
from gp2sm.place_clips import assign


def c(i, ts, r=1.5, order=None):
    return {"id": i, "ts": ts, "ratio": r, "order": order or str(i)}


def test_exact_time_and_shape_pairs():
    out = assign([c("clip", 1000)], [c("s1", 1000), c("s2", 1003)])
    assert out["clip"] == ("s1", 0, 2)


def test_aspect_must_match():
    out = assign([c("clip", 1000, 2.17)], [c("s1", 1000, 1.33)])
    assert out == {}


def test_outside_window_not_paired():
    assert assign([c("clip", 1000)], [c("s1", 1100)], window=60) == {}


def test_burst_pairs_one_to_one_in_name_order():
    clips = [c("cA", 500, order="a"), c("cB", 500, order="b"), c("cC", 500, order="c")]
    stills = [c("s1", 500, order="1"), c("s2", 500, order="2")]
    out = assign(clips, stills)
    assert out["cA"][0] == "s1" and out["cB"][0] == "s2" and "cC" not in out  # 3 clips, 2 stills


def test_mp4_dims_reads_video_track_skipping_audio():
    def tkhd(w, h):
        body = bytes([0, 0, 0, 0]) + b"\0" * 20 + b"\0" * 8 + b"\0" * 8 + b"\0" * 36
        return b"tkhd" + body + (w << 16).to_bytes(4, "big") + (h << 16).to_bytes(4, "big")
    data = b"...." + tkhd(0, 0) + tkhd(1920, 884)
    assert mp4_dims(data) == (1920, 884)
    assert round(aspect(884, 1920), 3) == round(1920 / 884, 3)
