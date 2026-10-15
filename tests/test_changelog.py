"""CHANGELOG.md's link references stay in step with its version headings.

Keep a Changelog headings are reference-style links: ``## [0.12.2]`` renders as
a link only if a matching ``[0.12.2]: <url>`` definition exists at the foot of
the file. The release ritual renames ``[Unreleased]`` to the new version and
never touched the footer, so seven consecutive releases (0.10.0-0.12.2) shipped
with dead headings and ``[Unreleased]`` still comparing from v0.9.1. A rule that
lives only in a ritual drifts; this pins it.
"""

from __future__ import annotations

import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
COMPARE = "https://github.com/kgruel/siftd/compare/"

_HEADING = re.compile(r"^## \[(?P<label>[^\]]+)\]", re.MULTILINE)
_LINK = re.compile(r"^\[(?P<label>[^\]]+)\]: (?P<url>\S+)$", re.MULTILINE)


def _headings() -> list[str]:
    return _HEADING.findall(CHANGELOG.read_text())


def _links() -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    for m in _LINK.finditer(CHANGELOG.read_text()):
        links.setdefault(m["label"], []).append(m["url"])
    return links


def test_every_heading_has_exactly_one_link_definition():
    links = _links()
    missing = [h for h in _headings() if h not in links]
    duplicated = sorted(label for label, urls in links.items() if len(urls) > 1)
    assert not missing, (
        f"CHANGELOG headings with no link definition at the foot: {missing}. "
        f"Add `[X.Y.Z]: {COMPARE}v<previous>...vX.Y.Z` when cutting a release."
    )
    assert not duplicated, f"CHANGELOG link labels defined more than once: {duplicated}"


def test_unreleased_compares_from_the_newest_release():
    versions = [h for h in _headings() if h != "Unreleased"]
    assert versions, "CHANGELOG has no version headings"
    expected = f"{COMPARE}v{versions[0]}...HEAD"
    assert _links().get("Unreleased") == [expected], (
        f"[Unreleased] must compare from the newest release heading ({versions[0]}): "
        f"expected `[Unreleased]: {expected}`"
    )


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_each_version_link_targets_itself_from_an_older_release():
    """Each ``[X]`` link is a compare range ending at ``vX`` and starting earlier.

    The start is not required to be the heading below: some tags (0.4.1, 0.4.2)
    shipped without a changelog section, so the right predecessor is the
    previous *tag*, which this file cannot see. The oldest release links to a
    tag page rather than a range and is skipped.
    """
    links = _links()
    wrong = []
    for version in [h for h in _headings() if h != "Unreleased"]:
        (url,) = links[version]
        if "/releases/tag/" in url:
            continue
        older, _, newer = url.removeprefix(f"{COMPARE}v").partition("...v")
        if newer != version or _version(older) >= _version(version):
            wrong.append(f"[{version}]: {url}")
    assert not wrong, "version links that do not span <older>...<this version>:\n" + "\n".join(wrong)
