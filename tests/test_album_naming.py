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
    assert naming.propose("2004-03-17 New puppy", T, today=TODAY).new == "2004-03 New puppy"
    assert naming.propose("2004-03-17 New puppy", T, keep_day=True, today=TODAY).new is None   # already conforms
    year = naming.propose("Christmas 2015", T, today=TODAY)
    assert (year.new, year.auto) == ("2015 Christmas", False)                 # medium: listed for review
    assert naming.propose("Christmas 2015", T, min_confidence="medium", today=TODAY).auto is True
    assert naming.propose("2019-06 Beach Trip", T, today=TODAY).note == "already follows the convention"
    us = naming.propose("Party 03-04-2021", T, today=TODAY)
    assert us.auto is False and "swapped" in us.note


def test_undated_names_use_tightly_clustered_photos_only():
    close = ["2021-07-02T10:00:00", "2021-07-04T09:00:00", "2021-07-05T18:00:00"]
    prop = naming.propose("Lake weekend", T, photo_dates=close, today=TODAY)
    assert (prop.new, prop.source, prop.confidence, prop.auto) == ("2021-07 Lake weekend", "photos", "medium", False)
    wide = ["2019-01-01", "2020-06-01", "2023-03-03"]
    prop = naming.propose("Kid's Auto Upload", T, photo_dates=wide, today=TODAY)
    assert prop.new is None and "span 1522 days" in prop.note
    assert naming.propose("Lake weekend", T, today=TODAY).note == "no date in the name"
