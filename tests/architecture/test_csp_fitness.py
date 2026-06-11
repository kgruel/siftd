"""CSP fitness functions (T1 + T2 of docs/guides/serve-browser-testing.md).

The serve CSP is ``script-src 'self' 'unsafe-inline'`` with **no**
``'unsafe-eval'``. htmx compiles three authored constructs via ``new Function``,
so any of them ships a feature that silently dies in the browser while every
TestClient test stays green (the original hx-on bug, commit a9f15240):

- ``hx-on:*=`` / ``hx-on=`` attributes
- ``hx-vals="js:..."``
- ``hx-trigger`` event filters (``keyup[key!='Enter']``)

T1 is static text scans (no imports, base lane) over the sources that emit
served HTML, plus a pin on the CSP itself so loosening the policy is a
deliberate, reviewed act. T2 renders the page shell and cross-checks every
resource reference against the parsed CSP directives — both directions.
Vendored assets are excluded — library *internals* are T3's job (the browser
smoke, ``./dev browser-smoke``), not a text scan's.
"""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

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


# ---------------------------------------------------------------------------
# T2 — HTML↔CSP cross-check: parse the rendered shell, validate every resource
# reference against the declared policy, both directions.
# ---------------------------------------------------------------------------

# HTML contexts that load a resource, mapped to the CSP directive governing
# them. ``preconnect`` is collected as a *reference* (direction B evidence that
# an allowed origin is really used — e.g. fonts.gstatic.com, whose font files
# are fetched by Google's CSS, not by our HTML) but is not itself CSP-governed,
# so it is skipped in direction A.
_DIRECTIVE_FOR_CONTEXT = {
    "script-src": "script-src",
    "stylesheet": "style-src",
    "img-src": "img-src",
}

# Resource directives whose absolute origins must be referenced by the shell.
# connect-src is excluded: it governs runtime fetches (auth.js's OIDC discovery
# + token exchange against the *configured* issuer), which no static HTML parse
# can witness.
_REFERENCED_DIRECTIVES = ("script-src", "style-src", "font-src", "img-src")


class _ShellResourceCollector(HTMLParser):
    """Collect every resource reference + inline-execution construct."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str]] = []  # (context, url)
        self.inline_scripts = 0
        self.inline_handlers: list[str] = []  # on* attributes
        self.inline_styles = 0  # style= attributes
        self._in_script_without_src = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            if a.get("src"):
                self.refs.append(("script-src", a["src"]))
            else:
                self._in_script_without_src = True
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href and "stylesheet" in rel:
                self.refs.append(("stylesheet", href))
            elif href and "preconnect" in rel:
                self.refs.append(("preconnect", href))
        elif tag == "img" and a.get("src"):
            self.refs.append(("img-src", a["src"]))
        for name, _value in attrs:
            if name.startswith("on"):
                self.inline_handlers.append(f"<{tag} {name}=...>")
            if name == "style":
                self.inline_styles += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script_without_src = False

    def handle_data(self, data):
        if self._in_script_without_src and data.strip():
            self.inline_scripts += 1
            self._in_script_without_src = False  # count each block once


def _parse_csp(csp: str) -> dict[str, list[str]]:
    out = {}
    for part in csp.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


def _origin_of(url: str) -> str | None:
    """Origin for absolute URLs; None for relative/self references."""
    p = urlparse(url)
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return None


def _rendered_shells() -> list[str]:
    """Every variant of the full-page shell (deep-link branches included)."""
    from siftd.serve.html_routes import _page_shell

    return [
        _page_shell(),
        _page_shell(conv_id="01ABCDEF"),
        _page_shell(search_q="hello world"),
        _page_shell(follow_sid="sid-1"),
    ]


def test_shell_resources_allowed_by_csp():
    """Direction A: every resource the shell references must be loadable.

    Catches the "added a CDN <script>/<link> but forgot the policy" drift —
    the asset 404s at the CSP layer in the browser while every TestClient
    test stays green. Relative URLs are 'self' and always allowed.
    """
    pytest.importorskip("litestar")
    from siftd.serve.app import _build_csp

    directives = _parse_csp(_build_csp(None))
    failures = []
    for html in _rendered_shells():
        c = _ShellResourceCollector()
        c.feed(html)
        for context, url in c.refs:
            if context == "preconnect":
                continue  # not CSP-governed; direction-B evidence only
            origin = _origin_of(url)
            if origin is None:
                continue  # relative → 'self'
            directive = _DIRECTIVE_FOR_CONTEXT[context]
            allowed = directives.get(directive, directives.get("default-src", []))
            if origin not in allowed:
                failures.append(f"{directive}: {url} (origin {origin} not in {allowed})")
    assert not failures, (
        "shell references resource(s) the CSP would block:\n" + "\n".join(sorted(set(failures)))
    )


def test_csp_origins_are_referenced_by_shell():
    """Direction B: every absolute origin the CSP allows must be used.

    A CSP allowance nothing references is stale attack surface — it survives
    asset removals silently (e.g. dropping Google Fonts from the shell should
    also drop fonts.googleapis.com/fonts.gstatic.com from the policy).
    """
    pytest.importorskip("litestar")
    from siftd.serve.app import _build_csp

    directives = _parse_csp(_build_csp(None))
    referenced = set()
    for html in _rendered_shells():
        c = _ShellResourceCollector()
        c.feed(html)
        referenced.update(o for _, url in c.refs if (o := _origin_of(url)))

    stale = []
    for directive in _REFERENCED_DIRECTIVES:
        for source in directives.get(directive, []):
            if source.startswith(("http://", "https://")) and source not in referenced:
                stale.append(f"{directive}: {source}")
    assert not stale, (
        "CSP allows origin(s) the shell never references (stale allowance):\n"
        + "\n".join(stale)
    )


def test_inline_constructs_match_csp():
    """The shell's inline scripts/handlers exist iff the CSP permits them.

    Today script-src and style-src carry 'unsafe-inline' and the shell uses
    inline blocks + onclick. If a future nonce/hash hardening drops
    'unsafe-inline', every inline construct must be reauthored in the same
    change — this is the test that forces that.
    """
    pytest.importorskip("litestar")
    from siftd.serve.app import _build_csp

    directives = _parse_csp(_build_csp(None))
    for html in _rendered_shells():
        c = _ShellResourceCollector()
        c.feed(html)
        if c.inline_scripts or c.inline_handlers:
            assert "'unsafe-inline'" in directives.get("script-src", []), (
                f"shell has {c.inline_scripts} inline script block(s) + handlers "
                f"{c.inline_handlers} but script-src lacks 'unsafe-inline'"
            )
        if c.inline_styles:
            assert "'unsafe-inline'" in directives.get("style-src", []), (
                f"shell has {c.inline_styles} style= attribute(s) but style-src "
                "lacks 'unsafe-inline'"
            )
