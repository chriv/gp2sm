"""Organize rules (pure): date chain, filename dates, grouping, album names, recency."""

import datetime

import pytest

from gp2sm.organize import rules

TODAY = datetime.date(2026, 10, 7)


@pytest.mark.parametrize("name, expected", [
    ("IMG_20230504_101112.jpg", ("2023-05-04T10:11:12", "camera_datetime")),
    ("PXL_20230504_101112345.jpg", ("2023-05-04T10:11:12", "camera_datetime")),
    ("20230504_101112.mp4", ("2023-05-04T10:11:12", "camera_datetime")),
    ("Screenshot 2023-05-04 at 10.11.12.png", ("2023-05-04T10:11:12", "screenshot")),
    ("Screen Shot 2023-05-04 at 10.11.12 AM.png", ("2023-05-04T10:11:12", "screenshot")),
    ("Screenshot_2023-05-04-10-11-12.png", ("2023-05-04T10:11:12", "screenshot")),
    ("Screen Shot 2023-05-04 at 1.11.12 PM.png", ("2023-05-04T13:11:12", "screenshot")),
    ("Screen Shot 2023-05-04 at 12.01.02 AM.png", ("2023-05-04T00:01:02", "screenshot")),
    ("IMG-20230504-WA0007.jpg", ("2023-05-04", "whatsapp")),
    ("Birthday 2023-05-04.jpg", ("2023-05-04", "iso_date")),
    ("IMG_1234.HEIC", (None, None)),                 # a counter, not a date
    ("IMG_20231304_101112.jpg", (None, None)),       # month 13
    ("IMG_20270101_000000.jpg", (None, None)),       # in the future
    ("scan_19850101_000000.jpg", (None, None)),      # before MIN_YEAR
    ("1e8606f2ee094c22b21dd053fb5b235f.mov", (None, None)),
])
def test_filename_dates(name, expected):
    assert rules.date_from_filename(name, TODAY) == expected


def test_date_chain_precedence_and_plugins():
    item = {"filename": "IMG_20230504_101112.jpg", "capture_time": "2022-01-02T03:04:05",
            "uploaded": "2024-06-01T12:00:00+00:00"}
    assert rules.date_for(item, ["camera", "filename"], "UTC", today=TODAY) == ("2022-01-02T03:04:05", "camera")
    assert rules.date_for(item, ["filename", "camera"], "UTC", today=TODAY) == ("2023-05-04T10:11:12",
                                                                               "filename:camera_datetime")
    bare = {"filename": "IMG_1.jpg", "capture_time": "0000:00:00 00:00:00", "uploaded": "2024-06-01T02:00:00+00:00"}
    assert rules.date_for(bare, ["camera", "filename"], "UTC", today=TODAY) == (None, "none")
    assert rules.date_for(bare, ["camera", "upload"], "America/New_York", today=TODAY) == ("2024-05-31T22:00:00", "upload")
    old = {"filename": "PICT0008.TIF", "capture_time": None, "album_name": "2004-03-17 New puppy (6 weeks old)"}
    assert rules.date_for(old, ["camera", "filename", "album"], "UTC", today=TODAY) == ("2004-03-17", "album:iso_date")
    plugin = {"legacy": lambda it: "2019-12-31T23:59:59"}
    assert rules.date_for(bare, ["legacy", "upload"], "UTC", plugins=plugin, today=TODAY)[1] == "legacy"
    with pytest.raises(ValueError):
        rules.date_for(bare, ["telepathy"], "UTC")


def test_grouping_first_match_wins_and_unassigned():
    r = [{"name": "Dad phone", "model": "iPhone 14*", "album": "*Dad*"},
         {"name": "Kid iPad", "model": "iPad*"},
         {"name": "Screenshots", "filename": "screenshot*"}]
    assert rules.group_for({"album": "Uploads/Dad", "model": "iPhone 14 Pro"}, r, "Unassigned") == "Dad phone"
    assert rules.group_for({"album": "Uploads/Mom", "model": "iPhone 14 Pro"}, r, "Unassigned") == "Unassigned"
    assert rules.group_for({"model": "iPad mini 4"}, r, "Unassigned") == "Kid iPad"
    assert rules.group_for({"filename": "Screenshot 2023.png", "model": None}, r, "Unassigned") == "Screenshots"


def test_album_names():
    assert rules.album_name("{group} {yyyy}-{mm}", "2023-05-04T10:00:00", "Kid iPad") == "Kid iPad 2023-05"
    assert rules.album_name("{yyyy}-{mm} {group}", "2023-05-04", "") == "2023-05"
    assert rules.album_name("Photos {yyyy}-{mm}", "2023-05-04", "ignored") == "Photos 2023-05"


def test_recent_uploads_are_skipped():
    now = datetime.datetime(2026, 10, 7, tzinfo=datetime.timezone.utc)
    assert rules.recent("2026-10-01T00:00:00+00:00", 14, now) is True
    assert rules.recent("2026-09-01T00:00:00+00:00", 14, now) is False
    assert rules.recent("2026-10-01T00:00:00+00:00", 0, now) is False
