"""Album name parsing and rename proposals (pure). Synthetic names only."""

import datetime

import pytest

from gp2sm.albums import naming

TODAY = datetime.date(2026, 10, 7)
T = {"month": "{yyyy}-{mm} {subject}", "year": "{yyyy} {subject}", "day": "{yyyy}-{mm}-{dd} {subject}"}


@pytest.mark.parametrize("name, iso, precision, subject, confidence", [
    ("2004-03-17 New puppy", "2004-03-17", "day", "New puppy", "high"),
    ("2004.03.17 - New puppy", "2004-03-17", "day", "New puppy", "high"),
    ("20040317 New puppy", "2004-03-17", "day", "New puppy", "high"),
    ("New puppy 2004-03", "2004-03", "month", "New puppy", "high"),
    ("Beach Trip 06-2019", "2019-06", "month", "Beach Trip", "high"),
    ("06/2019 Beach Trip", "2019-06", "month", "Beach Trip", "high"),
    ("March 2019 Science Fair", "2019-03", "month", "Science Fair", "high"),
    ("Science Fair Mar. 2019", "2019-03", "month", "Science Fair", "high"),
    ("Science Fair (Sept 2019)", "2019-09", "month", "Science Fair", "high"),
    ("17 March 2019 Science Fair", "2019-03-17", "day", "Science Fair", "high"),
    ("Science Fair March 17, 2019", "2019-03-17", "day", "Science Fair", "high"),
    ("Graduation 5/14/2022", "2022-05-14", "day", "Graduation", "high"),        # 14 can only be the day
    ("Party 03-04-2021", "2021-03-04", "day", "Party", "medium"),               # 3 Apr or 4 Mar: US order assumed
    ("Christmas 2015", "2015", "year", "Christmas", "medium"),
    ("2015 Christmas", "2015", "year", "Christmas", "medium"),
    ("Summer '19", "2019", "year", "Summer", "medium"),
    ("Camp Jul '18", "2018-07", "month", "Camp", "medium"),
    ("Route 66", None, "none", "Route 66", "none"),
    ("Apartment 1204", None, "none", "Apartment 1204", "none"),
    ("Top 100", None, "none", "Top 100", "none"),
    ("Kid's Auto Upload (2)", None, "none", "Kid's Auto Upload (2)", "none"),
    ("IMG_1234 scans", None, "none", "IMG_1234 scans", "none"),
    ("Room 2099", None, "none", "Room 2099", "none"),                          # beyond next year: not a date
    ("Part 2 of 3", None, "none", "Part 2 of 3", "none"),
    ("2023-13 Typo", "2023", "year", "13 Typo", "medium"),                    # no month 13; the year still counts (review)
])
def test_parse_table(name, iso, precision, subject, confidence):
    p = naming.parse(name, TODAY)
    assert (p.iso(), p.precision, p.subject, p.confidence) == (iso, precision, subject, confidence), p


def test_propose_formats_and_confidence():
    prop = naming.propose("Beach Trip 06-2019", T, today=TODAY)
    assert (prop.new, prop.auto, prop.source) == ("2019-06 Beach Trip", True, "name")
    assert naming.propose("2004-03-17 New puppy", T, today=TODAY).new is None        # keeps the day: already conforms
    assert naming.propose("2004-03-17 New puppy", T, keep_day=False, today=TODAY).new == "2004-03 New puppy"
    year = naming.propose("Christmas 2015", T, today=TODAY)
    assert (year.new, year.auto) == ("2015 Christmas", False)                 # medium: listed for review
    assert naming.propose("Christmas 2015", T, min_confidence="medium", today=TODAY).auto is True
    assert naming.propose("2019-06 Beach Trip", T, today=TODAY).note == "already follows the convention"
    us = naming.propose("Party 03-04-2021", T, today=TODAY)
    assert us.auto is False and "swapped" in us.note


def test_undated_names_use_tightly_clustered_photos_only():
    close = ["2021-07-02T10:00:00", "2021-07-04T09:00:00", "2021-07-05T18:00:00"]
    few = naming.propose("Lake weekend", T, photo_dates=close, today=TODAY)
    assert few.new is None and "fewer than 5 dated photos" in few.note
    close = close + ["2021-07-03T08:00:00", "2021-07-03T09:00:00"]
    prop = naming.propose("Lake weekend", T, photo_dates=close, today=TODAY)
    assert (prop.new, prop.source, prop.confidence, prop.auto) == ("2021-07 Lake weekend", "photos", "medium", False)
    wide = ["2019-01-01", "2020-06-01", "2023-03-03"]
    prop = naming.propose("Kid's Auto Upload", T, photo_dates=wide, min_photos=3, today=TODAY)
    assert prop.new is None and "span 1522 days" in prop.note
    assert naming.propose("Lake weekend", T, today=TODAY).note == "no date in the name"


def test_albums_uploaded_over_a_long_time_are_not_dated():
    close = ["2021-05-15", "2021-05-20", "2021-06-01", "2021-06-05", "2021-06-12"]
    prop = naming.propose("Phone Auto Upload", T, photo_dates=close, upload_dates=["2019-01-01", "2026-08-01"],
                          today=TODAY)
    assert prop.new is None and "uploaded over 2769 days" in prop.note
    ok = naming.propose("Lake weekend", T, photo_dates=close, upload_dates=["2021-06-13", "2021-06-14"], today=TODAY)
    assert ok.new == "2021-06 Lake weekend"


def test_page_starts_cover_the_whole_album():
    from gp2sm.albums.cli import page_starts
    starts = page_starts(5001, 60, seed="abc")
    assert len(starts) == 3 and starts == page_starts(5001, 60, seed="abc")
    assert starts[0] <= 1680 < starts[1] <= 3340 < starts[2] <= 5001
    assert page_starts(15, 60, seed="x") == [1]


def test_one_day_events_get_the_full_date():
    day = ["2008-09-14T10:00:00"] * 3 + ["2008-09-14T16:00:00"] * 3
    prop = naming.propose("Old House", T, photo_dates=day, today=TODAY)
    assert (prop.new, prop.confidence) == ("2008-09-14 Old House", "medium")
    assert naming.propose("Old House", T, photo_dates=day, keep_day=False, today=TODAY).new == "2008-09 Old House"


def test_year_only_names_gain_the_month_from_agreeing_photos():
    oct11 = ["2011-10-01", "2011-10-02", "2011-10-02", "2011-10-03", "2011-10-05"]
    prop = naming.propose("Cotton Pickin 2011", T, photo_dates=oct11, today=TODAY)
    assert (prop.new, prop.confidence, prop.auto, prop.source) == ("2011-10 Cotton Pickin", "high", True, "name+photos")
    other_year = ["2012-03-01"] * 5                       # photos disagree with the name: keep the name's year
    assert naming.propose("Cotton Pickin 2011", T, photo_dates=other_year, today=TODAY).new == "2011 Cotton Pickin"
    one_day = ["2011-10-02"] * 5
    assert naming.propose("Cotton Pickin 2011", T, photo_dates=one_day, today=TODAY).new == "2011-10-02 Cotton Pickin"


def test_words_left_dangling_by_the_date_are_dropped():
    assert naming.propose("Pat Portraits taken November 12, 2005", T).new == "2005-11-12 Pat Portraits"
    assert naming.propose("Pat Portraits Taken on August 03, 2005", T).new == "2005-08-03 Pat Portraits"
    assert naming.propose("Snow in December 2009", T).new == "2009-12 Snow"
    assert naming.propose("Day of Caring 2014", T).new == "2014 Day of Caring"        # "of" mid-name stays


def test_year_ranges_are_left_as_they_are():
    for name in ("Portraits 2019/2020", "Trip 2010-2012", "Archive 1999 – 2003"):
        p = naming.propose(name, T)
        assert p.new is None and "spans" in p.note
    assert naming.propose("Trip 2010-11", T).new == "2010-11 Trip"                    # a month, not a range


def test_a_year_range_album_is_dated_by_clustered_photos_inside_the_range():
    one_day = ["2020-04-05T10:00:00"] * 23
    p = naming.propose("Portraits 2019/2020", T, photo_dates=one_day)
    assert p.new == "2020-04-05 Portraits 2019/2020" and p.auto and "kept in the name" in p.note
    outside = ["2022-04-05T10:00:00"] * 23                                  # photos outside the named years
    assert naming.propose("Portraits 2019/2020", T, photo_dates=outside).new is None
    spread = [f"2020-{m:02d}-01T10:00:00" for m in range(1, 13)]             # a whole year: not one event
    assert naming.propose("Portraits 2019/2020", T, photo_dates=spread).new is None
