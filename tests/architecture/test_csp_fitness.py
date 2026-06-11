"""CSP fitness function (T1 of docs/guides/serve-browser-testing.md).

The serve CSP is ``script-src 'self' 'unsafe-inline'`` with **no**
``'unsafe-eval'``. htmx compiles three authored constructs via ``new Function``,
so any of them ships a feature that silently dies in the browser while every
TestClient test stays green (the original hx-on bug, commit a9f15240):

- ``hx-on:*=`` / ``hx-on=`` attributes
- ``hx-vals="js:..."``
- ``hx-trigger`` event filters (``keyup[key!='Enter']``)

These tests are static text scans (no imports, base lane) over the sources that
emit served HTML, plus a pin on the CSP itself so loosening the policy is a
deliberate, reviewed act. Vendored assets are excluded — library *internals*
are T3's job (the browser smoke), not a text scan's.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent.parent / "src" / "siftd"

# Every source that emits HTML the serve layer can return.
_HTML_EMITTING_SOURCES = [
    *sorted((SRC / "serve").rglob("*.py")),
    SRC / "output" / "html_fmt.py",
]

# First-party JS served under the CSP (vendored libraries excluded: T3 covers
# their internals in a real browser).
_FIRST_PARTY_JS = [
    p for p in sorted((SRC / "serve" / "static").glob("*.js"))
]

# Authored htmx constructs that require 'unsafe-eval'. Patterns match attribute
# *usage* (with a value assignment) so prose mentions in comments/docstrings —
# including rendered inline-script comments — don't false-positive.
_EVAL_REQUIRING = [
    (re.compile(r"hx-on:[a-zA-Z:-]*="), "hx-on: attribute (htmx 2 syntax)"),
    (re.compile(r"hx-on=[\"']"), "hx-on attribute (htmx 1 syntax)"),
    (re.compile(r"hx-vals=[\"']js:"), 'hx-vals="js:..." (evaluated expression)'),
    (re.compile(r"hx-trigger=\"[^\"]*\["), "hx-trigger event filter"),
    (re.compile(r"hx-trigger='[^']*\["), "hx-trigger event filter"),
]


def _scan(paths, patterns):
    hits = []
    for path in paths:
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in patterns:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(SRC.parent.parent)}:{lineno} — {label}: {line.strip()[:120]}")
    return hits


def test_no_eval_requiring_htmx_constructs():
    """No authored htmx construct may require 'unsafe-eval'.

    htmx compiles hx-on bodies, js: hx-vals, and trigger event filters via
    ``new Function``; under the serve CSP they throw a script-src violation in
    the browser and the feature silently breaks. Rewrite as addEventListener
    in an inline block or a static/*.js file, or pick an event type that makes
    the filter unnecessary (e.g. ``input`` instead of ``keyup[key!='Enter']``).
    """
    hits = _scan(_HTML_EMITTING_SOURCES, _EVAL_REQUIRING)
    assert not hits, (
        "eval-requiring htmx construct(s) authored while the CSP forbids "
        "'unsafe-eval':\n" + "\n".join(hits)
    )


def test_no_eval_in_first_party_js():
    """First-party static JS must not eval — same contract auth.js already keeps."""
    patterns = [
        (re.compile(r"\beval\s*\("), "eval()"),
        (re.compile(r"\bnew Function\s*\("), "new Function()"),
    ]
    assert _FIRST_PARTY_JS, "expected first-party JS under serve/static"
    hits = _scan(_FIRST_PARTY_JS, patterns)
    assert not hits, "eval in first-party JS:\n" + "\n".join(hits)


def test_csp_header_is_pinned():
    """Pin the exact baseline CSP so any policy change is deliberate.

    If this fails because the policy changed on purpose, update the pin in the
    same commit and say why in the commit body — and if 'unsafe-eval' is being
    added, the two tests above lose their premise and the whole tier needs
    rethinking, not just a pin bump.
    """
    pytest.importorskip("litestar")
    from siftd.serve.app import _build_csp

    assert _build_csp(None) == (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )
    # The issuer widening only ever touches connect-src — never script-src.
    widened = _build_csp({"issuer": "https://idp.example.com"})
    assert "connect-src 'self' https://idp.example.com" in widened
    assert "unsafe-eval" not in widened
