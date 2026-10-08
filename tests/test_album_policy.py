"""Album settings policy (pure): precedence, scope matching, drift, warnings, findings."""

from gp2sm.albums import policy

POLICIES = [
    {"scope": ["*"], "sort": "date_taken", "comments": True},
    {"scope": ["Family/*"], "exclude": ["*Auto Upload*"], "privacy": "private", "downloads": True,
     "download_size": "original"},
    {"scope": ["Family/Public*"], "privacy": "public", "downloads": False, "download_size": "large"},
]


def album(folder, name, **kw):
    return dict(folder=folder, name=name, **kw)


def test_last_matching_policy_wins_and_excludes_apply():
    want = policy.desired(album("Family", "2019 Beach"), POLICIES)
    assert want == {"sort": ("date_taken", 1), "comments": (True, 1), "privacy": ("private", 2),
                    "downloads": (True, 2), "download_size": ("original", 2)}
    assert "privacy" not in policy.desired(album("Family", "Kid's Auto Upload"), POLICIES)
    assert policy.desired(album("Family", "Public Gallery"), POLICIES)["privacy"] == ("public", 3)
    assert policy.desired(album("", "Loose album"), POLICIES) == {"sort": ("date_taken", 1), "comments": (True, 1)}


def test_drift_orders_downloads_first_and_skips_size_when_downloads_off():
    actual = {"sort": "position", "comments": True, "privacy": "private", "downloads": False,
              "download_size": "original", "effective_privacy": "private"}
    fixes, warnings = policy.drift(actual, policy.desired(album("Family", "2019 Beach"), POLICIES))
    assert [f["setting"] for f in fixes] == ["downloads", "sort"]
    assert warnings == []
    fixes, warnings = policy.drift(actual, policy.desired(album("Family", "Public Gallery"), POLICIES))
    assert [f["setting"] for f in fixes] == ["privacy", "sort"]
    assert any("download_size applies only with downloads on" in w for w in warnings)


def test_folder_privacy_that_overrides_the_album_is_a_warning():
    actual = {"privacy": "public", "effective_privacy": "private"}
    fixes, warnings = policy.drift(actual, {"privacy": ("public", 3)})
    assert fixes == [] and "containing folder makes it private" in warnings[0]


def test_findings():
    assert policy.findings({"item_count": 0}, 4000, 5000) == ["empty"]
    assert policy.findings({"item_count": 4500}, 4000, 5000) == ["near the item cap (4500 of 5000)"]
    assert policy.findings({"item_count": 5001}, 4000, 5000) == ["over the item cap (5001 > 5000)"]
    assert policy.findings({"item_count": 10}, 4000, 5000) == []


def test_an_album_private_by_its_own_setting_is_not_blamed_on_its_folder():
    want = {"privacy": ("public", 1)}
    own = {"privacy": "private", "effective_privacy": "private"}
    fixes, warnings = policy.drift(dict(own, folder_privacy="public"), want)
    assert [f["desired"] for f in fixes] == ["public"] and warnings == []            # the fix will take effect
    fixes, warnings = policy.drift(dict(own, folder_privacy="private"), want)
    assert "containing folder makes it private" in warnings[0]                      # the folder keeps it private
    assert policy.drift(own, want)[1] == []                                         # unknown folder: not blamed
    assert "containing folder" in policy.drift({"privacy": "unlisted", "effective_privacy": "private"}, want)[1][0]
