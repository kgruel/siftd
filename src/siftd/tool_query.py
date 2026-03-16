"""Parser for tool-oriented search queries.

Implements the roadmap MVP syntax:

    field:value bare terms here

Fielded tokens become structured filters; bare tokens remain free-text terms.
This module is intentionally execution-agnostic so the parser can be reused by
CLI, API, and future tool-search projection code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from siftd.dateparse import parse_date

_FIELD_RE = re.compile(r"^(?P<field>[a-z_][a-z0-9_-]*):(?P<value>.+)$", re.IGNORECASE)

# Initial roadmap vocabulary.
KNOWN_FIELDS = {
    "tool",
    "tool_family",
    "status",
    "path",
    "basename",
    "ext",
    "cmd",
    "pattern",
    "arg",
    "result",
    "result_status",
    "workspace",
    "tag",
    "all_tags",
    "no_tag",
    "tool_tag",
    "model",
    "since",
    "before",
    "provider",
    "harness",
}

TOOL_ALIASES = {
    "bash": "shell.execute",
    "shell": "shell.execute",
    "run_experiment": "shell.execute",
    "read": "file.read",
    "view": "file.read",
    "write": "file.write",
    "edit": "file.edit",
    "replace": "file.edit",
    "grep": "search.grep",
    "search_file_content": "search.grep",
    "glob": "file.glob",
    "list_directory": "file.glob",
    "google_web_search": "search.web",
    "web_fetch": "web.fetch",
    "ask_user": "ui.ask",
    "task": "task.spawn",
}

REVERSE_TOOL_ALIASES: dict[str, set[str]] = {}
for raw_name, canonical_name in TOOL_ALIASES.items():
    REVERSE_TOOL_ALIASES.setdefault(canonical_name, set()).add(raw_name)

# Add observed harness/raw aliases from adapter mappings so canonical queries can
# retrieve calls preserved under raw names without erasing provenance.
for raw_name, canonical_name in {
    "Read": "file.read",
    "Write": "file.write",
    "Edit": "file.edit",
    "Glob": "file.glob",
    "Bash": "shell.execute",
    "Grep": "search.grep",
    "WebSearch": "search.web",
    "WebFetch": "web.fetch",
    "Task": "task.spawn",
    "TaskOutput": "task.output",
    "KillShell": "task.kill",
    "AskUserQuestion": "ui.ask",
    "TodoWrite": "ui.todo",
    "NotebookEdit": "notebook.edit",
    "Skill": "skill.invoke",
    "shell_command": "shell.execute",
    "exec_command": "shell.execute",
    "apply_patch": "file.edit",
    "update_plan": "ui.todo",
    "view_image": "file.read",
    "write_stdin": "shell.stdin",
    "read_file": "file.read",
    "write_file": "file.write",
    "edit_file": "file.edit",
    "run_shell_command": "shell.execute",
    "search_files": "search.grep",
    "list_files": "file.glob",
}.items():
    REVERSE_TOOL_ALIASES.setdefault(canonical_name, set()).add(raw_name)

FIELD_ALIASES = {
    "all-tag": "all_tags",
    "all-tags": "all_tags",
    "all_tag": "all_tags",
    "no-tag": "no_tag",
    "tool-tag": "tool_tag",
}


@dataclass(frozen=True)
class ToolQueryTerm:
    """Single parsed token from a tool query string."""

    raw: str
    field: str | None = None
    value: str | None = None

    @property
    def is_fielded(self) -> bool:
        return self.field is not None


@dataclass(frozen=True)
class ToolQuery:
    """Structured representation of a parsed tool query."""

    raw: str
    terms: list[ToolQueryTerm] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)
    bare_terms: list[str] = field(default_factory=list)
    unknown_fields: dict[str, list[str]] = field(default_factory=dict)

    @property
    def free_text(self) -> str:
        """Bare terms rejoined for FTS-style ranking."""
        return " ".join(self.bare_terms)

    @property
    def has_fields(self) -> bool:
        return bool(self.fields or self.unknown_fields)


def normalize_tool_name(value: str) -> str:
    """Expand common user-facing aliases to canonical tool names."""
    return TOOL_ALIASES.get(value.lower(), value)


def expand_tool_names_for_matching(value: str) -> list[str]:
    """Expand a tool query value to canonical + known raw aliases.

    This lets queries like ``tool:shell.execute`` match calls stored under raw
    names such as ``bash`` or ``run_experiment`` while preserving raw tool
    provenance in storage and display.
    """
    canonical = normalize_tool_name(value)
    variants = {canonical, value}
    variants.update(REVERSE_TOOL_ALIASES.get(canonical, set()))
    return sorted(v for v in variants if v)


def normalize_field_name(value: str) -> str:
    """Normalize inline field names to a canonical underscore form."""
    lowered = value.lower()
    return FIELD_ALIASES.get(lowered, lowered.replace("-", "_"))


def normalize_field_value(field: str, value: str) -> str:
    """Normalize inline field values for fields with shared semantics."""
    if field == "tool":
        return normalize_tool_name(value)
    if field in {"since", "before"}:
        parsed = parse_date(value)
        return parsed or value
    return value


def build_fts5_query(terms: list[str]) -> str:
    """Build a safe FTS5 MATCH query from raw bare terms.

    Quotes each term individually so shell-ish input like ``./dev`` or
    ``pyproject.toml`` does not trigger FTS parser syntax errors.

    Punctuation-only terms are dropped as low-signal lexical noise. This keeps
    structured filters usable even when the free-text portion contains tokens
    like ``...`` or ``()`` that are unlikely to have meaningful FTS behavior.
    """
    cleaned = [term.strip() for term in terms if term and term.strip() and _is_signal_term(term)]
    return " ".join(_quote_fts5_term(term) for term in cleaned)


def _is_signal_term(term: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", term))


def _quote_fts5_term(term: str) -> str:
    safe = term.replace('"', '""')
    return f'"{safe}"'


def parse_tool_query(query: str, *, known_fields: set[str] | None = None) -> ToolQuery:
    """Parse a tool-oriented query string.

    Semantics:
    - ``field:value`` tokens become fielded terms
    - bare tokens remain free-text terms
    - known fields land in ``fields``
    - syntactically fielded but unknown keys land in ``unknown_fields``

    The parser intentionally stays tiny for v1:
    - no boolean grammar
    - no nested expressions
    - no escaping/quoted multi-token values beyond whatever the caller already
      preserved in the incoming string
    """
    known = {f.lower() for f in (known_fields or KNOWN_FIELDS)}
    raw = query.strip()
    if not raw:
        return ToolQuery(raw=query)

    parsed_terms: list[ToolQueryTerm] = []
    fields: dict[str, list[str]] = {}
    unknown_fields: dict[str, list[str]] = {}
    bare_terms: list[str] = []

    for token in raw.split():
        match = _FIELD_RE.match(token)
        if not match:
            parsed_terms.append(ToolQueryTerm(raw=token))
            bare_terms.append(token)
            continue

        field = normalize_field_name(match.group("field"))
        value = normalize_field_value(field, match.group("value"))
        parsed_terms.append(ToolQueryTerm(raw=token, field=field, value=value))

        target = fields if field in known else unknown_fields
        target.setdefault(field, []).append(value)

    return ToolQuery(
        raw=query,
        terms=parsed_terms,
        fields=fields,
        bare_terms=bare_terms,
        unknown_fields=unknown_fields,
    )
