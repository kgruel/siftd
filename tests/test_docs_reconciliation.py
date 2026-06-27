"""Reconciliation guards for the published documentation surface.

A standing version of the manual docs sweep: every published doc must be
reachable from an index, every local link must resolve, and every in-page
anchor must point at a real heading. Scope is the *published* surface — README
plus docs/ excluding the gitignored docs/dev/ working-notes tree.

These catch the recurring classes the one-off sweep found (an orphaned
reference doc, a dangling self-anchor) so they don't silently come back.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOCS = _REPO / "docs"
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")

# Reachability roots — the index entry points a reader starts from.
_ROOTS = ["README.md", "docs/index.md", "docs/concepts/index.md"]

# Published docs deliberately not linked from any index: contributor/operator
# working material the maintainer chose to keep off the landing page. Keep this
# honest — link a doc in, or add it here with a reason.
_INTENTIONALLY_UNINDEXED = {
    "docs/guides/serve-browser-testing.md",  # contributor: serve CSP test methodology
    "docs/guides/snapshot-policy.md",        # contributor: CLI-help snapshot update policy
    "docs/ops/homelab.md",                   # operator: self-hosting runbook
}


def _published_docs() -> list[str]:
    out = ["README.md"]
    for p in sorted(_DOCS.rglob("*.md")):
        rel = p.relative_to(_REPO).as_posix()
        if rel.startswith("docs/dev/"):
            continue  # gitignored working notes — not part of the published surface
        out.append(rel)
    return out


def _slug(text: str) -> str:
    text = text.replace("`", "").lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def _links(rel: str) -> list[str]:
    return _LINK_RE.findall((_REPO / rel).read_text(encoding="utf-8"))


def _resolve(src_rel: str, target: str) -> str | None:
    """Resolve a link target to a repo-relative path, or None if non-local."""
    if _URL_RE.match(target) or target.startswith("mailto:"):
        return None
    path = target.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return None  # pure in-page anchor
    resolved = ((_REPO / src_rel).parent / path).resolve()
    try:
        return resolved.relative_to(_REPO).as_posix()
    except ValueError:
        return None  # outside the repo — out of scope


def test_no_dead_local_links():
    dead = [
        f"{rel} -> {target}"
        for rel in _published_docs()
        for target in _links(rel)
        if (dest := _resolve(rel, target)) is not None and not (_REPO / dest).exists()
    ]
    assert not dead, "dead local links:\n  " + "\n  ".join(dead)


def test_no_dead_anchors():
    bad = []
    for rel in _published_docs():
        for target in _links(rel):
            if "#" not in target or _URL_RE.match(target):
                continue
            path, frag = target.split("#", 1)
            if not frag:
                continue
            dest = _resolve(rel, target) if path else rel
            if dest is None or not dest.endswith(".md") or not (_REPO / dest).exists():
                continue
            headings = {_slug(h) for h in _HEADING_RE.findall((_REPO / dest).read_text())}
            if _slug(frag) not in headings:
                bad.append(f"{rel} -> #{frag} (target {dest})")
    assert not bad, "dead in-page anchors:\n  " + "\n  ".join(bad)


def test_no_orphan_docs():
    docs = set(_published_docs())
    reach = set(_ROOTS)
    frontier = list(_ROOTS)
    while frontier:
        cur = frontier.pop()
        if not (_REPO / cur).exists():
            continue
        for target in _links(cur):
            dest = _resolve(cur, target)
            if dest and dest.endswith(".md") and dest in docs and dest not in reach:
                reach.add(dest)
                frontier.append(dest)
    orphans = sorted(d for d in docs if d not in reach and d not in _INTENTIONALLY_UNINDEXED)
    assert not orphans, (
        "published docs unreachable from any index — link them in, or add to "
        "_INTENTIONALLY_UNINDEXED with a reason:\n  " + "\n  ".join(orphans)
    )
