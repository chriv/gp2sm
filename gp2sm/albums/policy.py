"""Album settings policy (pure): what each album's settings should be, and how they differ.

Policies are ordered `[[policy]]` entries: `scope` (and optional `exclude`) globs matched case-insensitively
against an album's "folder/name" and its name, plus neutral settings. For each setting the last matching
policy wins, so general rules go first and specific ones after.

drift() compares an album's settings with what its policies want:
  - download_size is only compared when downloads should be on (turning downloads off resets it)
  - privacy is the album's own setting; when its folder makes it effectively private anyway, a less
    private policy can't take effect from the album, so that's reported as a warning, not a fix
findings() adds checks that aren't settings: empty albums, and albums near or over the item cap.
"""

import fnmatch

from gp2sm.services.base import ALBUM_SETTING_VALUES

RANK = {"public": 0, "unlisted": 1, "private": 2}


def path_of(album):
    return "/".join(p for p in (album.get("folder"), album.get("name")) if p)


def matches(album, patterns):
    path, name = path_of(album).lower(), (album.get("name") or "").lower()
    return any(fnmatch.fnmatchcase(path, p.lower()) or fnmatch.fnmatchcase(name, p.lower()) for p in patterns)


def desired(album, policies):
    """{setting: (value, policy number)} for an album (dict with folder, name)."""
    out = {}
    for n, pol in enumerate(policies, 1):
        if matches(album, pol.get("scope", [])) and not matches(album, pol.get("exclude", [])):
            for k, v in pol.items():
                if k in ALBUM_SETTING_VALUES:
                    out[k] = (v, n)
    return out


def drift(actual, want):
    """(fixes, warnings): fixes = [dict(setting, actual, desired, policy)] in a safe order (downloads first);
    warnings = [str]."""
    fixes, warnings = [], []
    order = sorted(want, key=lambda k: (k != "downloads", k))
    for k in order:
        value, n = want[k]
        if k == "download_size":
            downloads_wanted = want.get("downloads", (actual.get("downloads"), None))[0]
            if not downloads_wanted:
                warnings.append(f"policy {n}: download_size applies only with downloads on; not checked")
                continue
        if actual.get(k) != value:
            fixes.append({"setting": k, "actual": actual.get(k), "desired": value, "policy": n})
    if "privacy" in want and actual.get("effective_privacy") and \
            RANK.get(actual["effective_privacy"], 0) > RANK.get(want["privacy"][0], 0):
        warnings.append(f"a containing folder makes it {actual['effective_privacy']}; the album's own "
                        f"'{want['privacy'][0]}' setting can't take effect")
    return fixes, warnings


def findings(album, soft_cap, hard_cap):
    """Non-setting checks for an album dict with item_count."""
    count = album.get("item_count")
    if count is None:
        return []
    if count == 0:
        return ["empty"]
    if count > hard_cap:
        return [f"over the item cap ({count} > {hard_cap})"]
    if count >= soft_cap:
        return [f"near the item cap ({count} of {hard_cap})"]
    return []
