"""Adapter fixtures carry no personal data, so captured transcripts can be committed.

Golden fixtures are increasingly captured from real tool logs rather than written
by hand, because hand-written ones encode a format the tool never produces (the
vscode fixtures used field names real data never has, so the adapter's total loss
of assistant answers passed CI). A capture starts as someone's real transcript,
and this repo is public. Redaction is enforced here rather than left to review:
any home-directory path that names a real user, any email address outside the
reserved example domains, and any credential-shaped string fails.

Scope is the input fixtures adapters parse: ``tests/fixtures/adapters/**`` and
the top-level ``tests/fixtures/*`` transcripts. The schema dumps under
``tests/fixtures/schemas/`` are frozen captures of DDL and are out of scope.
"""

from __future__ import annotations

import re
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Home directories a redacted capture may use. Anything else under /Users or
# /home is presumed to be the capturing machine's real account.
_PLACEHOLDER_USERS = {"user", "test", "dev", "example", "alice", "bob", "me"}
_HOME = re.compile(r"(?:/Users/|/home/|C:\\\\Users\\\\|C:\\Users\\)([A-Za-z0-9._-]+)")
# A home directory encoded with dashes, as Claude Code and Codex name project folders
# (-Users-<name>-Code-...). It names the same account as the path it was encoded from.
_DASHED_HOME = re.compile(r"-Users-([A-Za-z0-9._]+)-")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
# RFC 2606 reserved domains, plus addresses that name no person.
_ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}
_ALLOWED_EMAIL_LOCALS = {"git", "noreply"}

_SECRETS = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic
    r"|sk-[A-Za-z0-9]{32,}"  # OpenAI-style
    r"|gh[pousr]_[A-Za-z0-9]{30,}"  # GitHub tokens
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|AIza[0-9A-Za-z_-]{35}"  # Google API key
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _scanned() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file() and "schemas" not in p.relative_to(FIXTURES).parts)


def _findings(text: str) -> list[str]:
    found = []
    for m in _HOME.finditer(text):
        if m.group(1).lower() not in _PLACEHOLDER_USERS:
            found.append(f"home path {m.group(0)!r}")
    for m in _DASHED_HOME.finditer(text):
        if m.group(1).lower() not in _PLACEHOLDER_USERS:
            found.append(f"dashed home path {m.group(0)!r}")
    for m in _EMAIL.finditer(text):
        local = m.group(0).split("@", 1)[0].lower()
        if m.group(1).lower() not in _ALLOWED_EMAIL_DOMAINS and local not in _ALLOWED_EMAIL_LOCALS:
            found.append(f"email {m.group(0)!r}")
    found.extend(f"credential-shaped {m.group(0)[:12]!r}..." for m in _SECRETS.finditer(text))
    return found


def test_fixtures_carry_no_personal_data():
    files = _scanned()
    assert any(p.parts[-3:-1] == ("claude_code", "minimal") for p in files), (
        "positive control: the scan reached the adapter fixtures"
    )
    offenders = {}
    for p in files:
        hits = _findings(p.read_text(errors="replace"))
        if hits:
            offenders[str(p.relative_to(FIXTURES))] = hits[:5]
    assert not offenders, (
        f"fixtures with unredacted personal data: {offenders}. Replace home paths with "
        f"/Users/user, emails with @example.com, and drop credentials before committing a capture."
    )


def test_the_scanner_sees_what_it_checks():
    must_flag = [
        "/Users/kaygee/Code/siftd",
        "/home/jdoe/.claude",
        "/Users/user/.subtask/workspaces/-Users-kaygee-Code-gruel.network--2",
        "mail jane.doe@gmail.com",
        "sk-ant-api03-abcdefghijklmnopqrstuv",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
    ]
    must_pass = [
        "/Users/user/project",
        "/home/test/.codex",
        "someone@example.com",
        "git@github.com:org/repo.git",
        "sk-short",
    ]
    missed = [s for s in must_flag if not _findings(s)]
    overflagged = [s for s in must_pass if _findings(s)]
    assert not missed and not overflagged, f"scanner misses {missed} or over-flags {overflagged}"
