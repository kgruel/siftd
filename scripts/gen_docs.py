#!/usr/bin/env python3
"""Generate reference documentation from code.

Usage:
    python scripts/gen_docs.py                 # Generate all docs
    python scripts/gen_docs.py api             # Generate API reference only
    python scripts/gen_docs.py schema          # Generate schema reference only
    python scripts/gen_docs.py cli             # Generate CLI reference only
    python scripts/gen_docs.py config          # Generate config reference only
    python scripts/gen_docs.py readmes         # Fill generated spans in per-folder READMEs
    python scripts/gen_docs.py readmes --list  # List managed README paths (one per line)
    python scripts/gen_docs.py readmes --bootstrap  # Create missing managed READMEs

Flags:
    --strict     Fail (nonzero exit) if any target skips instead of degrading
                 gracefully. Used by `./dev docs --check` so a skipped target
                 (e.g. api.md when optional deps are missing) is never a false
                 green.
    --bootstrap  For the readmes target: create any missing managed README from
                 a placeholder shell before filling its generated spans.
    --list       For the readmes target: print managed README paths and exit.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import sys
from pathlib import Path
from typing import Any, get_type_hints

REPO_ROOT = Path(__file__).parent.parent

# Ensure src is importable when running from repo root
sys.path.insert(0, str(REPO_ROOT / "src"))

DOCS_DIR = REPO_ROOT / "docs" / "reference"


def escape_pipe(s: str) -> str:
    """Escape pipe characters for markdown tables."""
    return s.replace("|", "\\|")


# =============================================================================
# API Reference Generation
# =============================================================================


def format_type(t: Any) -> str:
    """Format a type annotation as a readable string."""
    if t is type(None):
        return "None"
    if hasattr(t, "__origin__"):
        # Generic types like list[str], dict[str, int], etc.
        origin = t.__origin__
        args = getattr(t, "__args__", ())
        origin_name = getattr(origin, "__name__", str(origin))
        if origin_name == "UnionType" or str(origin) == "typing.Union":
            # Handle X | None style
            formatted = " | ".join(format_type(a) for a in args)
            return formatted
        if args:
            arg_str = ", ".join(format_type(a) for a in args)
            return f"{origin_name}[{arg_str}]"
        return origin_name
    if hasattr(t, "__name__"):
        return t.__name__
    # Normalize Python-version-specific module reprs so generated docs are stable
    # across interpreters: 3.13 relocated pathlib.Path to pathlib._local.Path, so a
    # `Path | None` union str()s differently on 3.13 vs the 3.12 publish env. Pin the
    # public name (the str() fallback handles unions, which lack __name__).
    return str(t).replace("typing.", "").replace("pathlib._local.", "pathlib.")


def parse_docstring(doc: str | None) -> dict[str, Any]:
    """Parse Google-style docstring into sections."""
    if not doc:
        return {"summary": "", "args": {}, "returns": "", "raises": {}}

    lines = doc.strip().split("\n")
    result: dict[str, Any] = {"summary": "", "args": {}, "returns": "", "raises": {}}

    # First paragraph is summary
    summary_lines = []
    i = 0
    while i < len(lines) and lines[i].strip() and not lines[i].strip().endswith(":"):
        summary_lines.append(lines[i].strip())
        i += 1
    result["summary"] = " ".join(summary_lines)

    # Parse sections
    current_section = None
    current_key = None
    current_value: list[str] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Section headers
        if stripped == "Args:":
            current_section = "args"
            current_key = None
        elif stripped == "Returns:":
            current_section = "returns"
            current_key = None
        elif stripped == "Raises:":
            current_section = "raises"
            current_key = None
        elif stripped == "Attributes:":
            current_section = "args"  # Treat attributes like args
            current_key = None
        elif stripped == "Example:" or stripped == "Examples:":
            current_section = "example"
            current_key = None
        elif current_section == "args" and stripped:
            # Pattern: name: description or name (type): description
            match = re.match(r"(\w+)(?:\s*\([^)]+\))?:\s*(.*)", stripped)
            if match:
                if current_key:
                    result["args"][current_key] = " ".join(current_value)
                current_key = match.group(1)
                current_value = [match.group(2)] if match.group(2) else []
            elif current_key:
                current_value.append(stripped)
        elif current_section == "returns" and stripped:
            if result["returns"]:
                result["returns"] += " " + stripped
            else:
                result["returns"] = stripped
        elif current_section == "raises" and stripped:
            match = re.match(r"(\w+):\s*(.*)", stripped)
            if match:
                if current_key:
                    result["raises"][current_key] = " ".join(current_value)
                current_key = match.group(1)
                current_value = [match.group(2)] if match.group(2) else []
            elif current_key:
                current_value.append(stripped)

        i += 1

    # Flush last item
    if current_section == "args" and current_key:
        result["args"][current_key] = " ".join(current_value)
    elif current_section == "raises" and current_key:
        result["raises"][current_key] = " ".join(current_value)

    return result


def format_dataclass(name: str, cls: type) -> str:
    """Format a dataclass as markdown."""
    lines = [f"### {name}", ""]

    doc = parse_docstring(cls.__doc__)
    if doc["summary"]:
        lines.append(doc["summary"])
        lines.append("")

    # Get fields with type hints
    try:
        hints = get_type_hints(cls)
    except Exception:
        hints = {}

    fields = dataclasses.fields(cls)
    if fields:
        lines.append("| Field | Type | Description |")
        lines.append("|-------|------|-------------|")

        for field in fields:
            field_type = hints.get(field.name, field.type)
            type_str = escape_pipe(format_type(field_type))
            desc = doc["args"].get(field.name, "")
            lines.append(f"| `{field.name}` | `{type_str}` | {escape_pipe(desc)} |")

        lines.append("")

    return "\n".join(lines)


def format_function(name: str, func: Any) -> str:
    """Format a function as markdown."""
    lines = [f"### {name}", ""]

    doc = parse_docstring(func.__doc__)
    if doc["summary"]:
        lines.append(doc["summary"])
        lines.append("")

    # Signature
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
    except Exception:
        sig = None
        hints = {}

    if sig:
        # Build signature string
        params = []
        for pname, param in sig.parameters.items():
            ptype = hints.get(pname)
            if param.kind == param.KEYWORD_ONLY:
                prefix = ""
            else:
                prefix = ""

            if ptype:
                type_str = format_type(ptype)
                if param.default is not param.empty:
                    params.append(f"{pname}: {type_str} = ...")
                else:
                    params.append(f"{pname}: {type_str}")
            elif param.default is not param.empty:
                params.append(f"{pname}=...")
            else:
                params.append(pname)

        return_type = hints.get("return")
        return_str = f" -> {format_type(return_type)}" if return_type else ""

        # Check if we need keyword-only marker
        has_kw_only = any(
            p.kind == p.KEYWORD_ONLY for p in sig.parameters.values()
        )
        if has_kw_only:
            # Find split point
            kw_idx = next(
                i
                for i, p in enumerate(sig.parameters.values())
                if p.kind == p.KEYWORD_ONLY
            )
            before = params[:kw_idx]
            after = params[kw_idx:]
            if before:
                param_str = ", ".join(before) + ", *, " + ", ".join(after)
            else:
                param_str = "*, " + ", ".join(after)
        else:
            param_str = ", ".join(params)

        lines.append("```python")
        lines.append(f"def {name}({param_str}){return_str}")
        lines.append("```")
        lines.append("")

    # Args table if present
    if doc["args"]:
        lines.append("**Parameters:**")
        lines.append("")
        for arg_name, arg_desc in doc["args"].items():
            lines.append(f"- `{arg_name}`: {arg_desc}")
        lines.append("")

    # Returns
    if doc["returns"]:
        lines.append(f"**Returns:** {doc['returns']}")
        lines.append("")

    # Raises
    if doc["raises"]:
        lines.append("**Raises:**")
        lines.append("")
        for exc, desc in doc["raises"].items():
            lines.append(f"- `{exc}`: {desc}")
        lines.append("")

    return "\n".join(lines)


def generate_api_docs() -> str | None:
    """Generate API reference documentation.

    Returns None when any symbol could not be resolved (e.g. missing optional
    deps like numpy) so the caller skips writing api.md and leaves the
    committed version canonical.
    """
    from siftd import api

    skipped: list[str] = []

    lines = [
        "# API Reference",
        "",
        "_Auto-generated from source code._",
        "",
        "## Overview",
        "",
        "The `siftd.api` module provides programmatic access to siftd functionality.",
        "CLI commands are thin wrappers over these functions.",
        "",
        "```python",
        "from siftd import api",
        "```",
        "",
    ]

    # Group exports by category (from __all__ comments in api/__init__.py)
    categories: dict[str, list[str]] = {
        "Adapters": [],
        "Doctor": [],
        "Peek": [],
        "Conversations": [],
        "Query Files": [],
        "File Refs": [],
        "Resources": [],
        "Search": [],
        "Stats": [],
        "Tools": [],
        "Export": [],
    }

    # Map names to categories based on their source module
    name_to_category = {}
    for name in api.__all__:
        try:
            obj = getattr(api, name)
        except ModuleNotFoundError as exc:
            skipped.append(f"{name} ({exc.name} not installed)")
            continue
        module = getattr(obj, "__module__", "")
        if "adapters" in module:
            name_to_category[name] = "Adapters"
        elif "doctor" in module:
            name_to_category[name] = "Doctor"
        elif "peek" in module:
            name_to_category[name] = "Peek"
        elif "conversations" in module:
            if "Query" in name:
                name_to_category[name] = "Query Files"
            else:
                name_to_category[name] = "Conversations"
        elif "file_refs" in module:
            name_to_category[name] = "File Refs"
        elif "resources" in module:
            name_to_category[name] = "Resources"
        elif "search" in module:
            name_to_category[name] = "Search"
        elif "stats" in module:
            name_to_category[name] = "Stats"
        elif "tools" in module:
            name_to_category[name] = "Tools"
        elif "export" in module:
            name_to_category[name] = "Export"
        else:
            name_to_category[name] = "Other"

    for name in api.__all__:
        cat = name_to_category.get(name, "Other")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(name)

    # Generate docs per category
    for category, names in categories.items():
        if not names:
            continue

        lines.append(f"## {category}")
        lines.append("")

        # Separate types and functions
        types = []
        functions = []
        exceptions = []

        for name in names:
            try:
                obj = getattr(api, name)
            except ModuleNotFoundError as exc:
                skipped.append(f"{name} ({exc.name} not installed)")
                continue
            if isinstance(obj, type):
                if issubclass(obj, Exception):
                    exceptions.append((name, obj))
                elif dataclasses.is_dataclass(obj):
                    types.append((name, obj))
                else:
                    types.append((name, obj))
            elif callable(obj):
                functions.append((name, obj))

        # Types first
        if types:
            lines.append("### Data Types")
            lines.append("")
            for name, cls in types:
                if dataclasses.is_dataclass(cls):
                    lines.append(format_dataclass(name, cls))
                else:
                    # Non-dataclass class
                    doc = parse_docstring(cls.__doc__)
                    lines.append(f"#### {name}")
                    lines.append("")
                    if doc["summary"]:
                        lines.append(doc["summary"])
                        lines.append("")

        # Exceptions
        if exceptions:
            lines.append("### Exceptions")
            lines.append("")
            for name, cls in exceptions:
                doc = parse_docstring(cls.__doc__)
                lines.append(f"#### {name}")
                lines.append("")
                if doc["summary"]:
                    lines.append(doc["summary"])
                else:
                    lines.append(f"Exception class for {category.lower()} errors.")
                lines.append("")

        # Functions
        if functions:
            lines.append("### Functions")
            lines.append("")
            for name, func in functions:
                lines.append(format_function(name, func))

    if skipped:
        print(
            "warning: api.md not regenerated — skipped symbols: "
            + ", ".join(skipped),
            file=sys.stderr,
        )
        return None
    return "\n".join(lines)


# =============================================================================
# Schema Reference Generation
# =============================================================================


def parse_schema(sql: str) -> list[dict]:
    """Parse schema.sql into structured sections."""
    sections = []

    # Split by section markers: line of dashes, then comment lines, then line of dashes
    # Pattern: 80+ dashes, newline, one or more comment lines, 80+ dashes
    section_pattern = re.compile(
        r"^-{10,}\n((?:-- .+\n)+)-{10,}$", re.MULTILINE
    )

    parts = section_pattern.split(sql)
    # parts[0] is header, then alternating: section_comment_block, section_body

    for i in range(1, len(parts), 2):
        # Extract section name from first comment line
        comment_block = parts[i].strip()
        first_line = comment_block.split("\n")[0]
        section_name = first_line.lstrip("- ").strip()

        section_body = parts[i + 1] if i + 1 < len(parts) else ""

        tables = parse_tables(section_body)
        if tables:
            sections.append({"name": section_name, "tables": tables})

    return sections


def parse_tables(sql: str) -> list[dict]:
    """Parse CREATE TABLE statements from SQL."""
    tables = []

    # Match CREATE TABLE with body
    table_pattern = re.compile(
        r"(?:--\s*(.+?)\n)?CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*"
        r"(?:USING\s+(\w+))?\s*\(([^;]+)\)",
        re.IGNORECASE | re.DOTALL,
    )

    for match in table_pattern.finditer(sql):
        comment = match.group(1)
        name = match.group(2)
        using = match.group(3)  # For VIRTUAL TABLE USING fts5
        body = match.group(4)

        if using:
            # Virtual table (FTS5)
            tables.append({
                "name": name,
                "comment": comment,
                "virtual": using,
                "columns": parse_fts5_columns(body),
            })
        else:
            tables.append({
                "name": name,
                "comment": comment,
                "columns": parse_columns(body),
            })

    return tables


def parse_columns(body: str) -> list[dict]:
    """Parse column definitions from table body."""
    columns = []

    # Split by lines first to handle multiline entries properly
    lines = body.strip().split("\n")

    # Reassemble into column definitions (join lines that are continuations)
    column_defs = []
    current_def = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this starts a new column definition (name followed by type)
        # or is a table constraint
        is_new_col = re.match(r"^(\w+)\s+(TEXT|INTEGER|REAL|BLOB)\b", stripped, re.I)
        is_constraint = stripped.upper().startswith(
            ("PRIMARY KEY", "UNIQUE (", "FOREIGN KEY", "CHECK")
        )

        if is_new_col or is_constraint:
            if current_def:
                column_defs.append(" ".join(current_def))
            current_def = [stripped]
        else:
            # Continuation of previous definition
            current_def.append(stripped)

    if current_def:
        column_defs.append(" ".join(current_def))

    for col_def in column_defs:
        # Skip table-level constraints
        if col_def.upper().startswith(("PRIMARY KEY", "UNIQUE (", "FOREIGN KEY", "CHECK")):
            continue

        # Parse: name TYPE [constraints] [-- comment]
        # Handle inline comments that may contain commas
        comment = ""
        if "--" in col_def:
            col_part, comment = col_def.split("--", 1)
            comment = comment.strip()
        else:
            col_part = col_def

        # Remove trailing comma
        col_part = col_part.rstrip(",").strip()

        # Match: name TYPE [optional constraints]
        col_match = re.match(
            r"^(\w+)\s+(TEXT|INTEGER|REAL|BLOB)\s*(.*?)$",
            col_part,
            re.IGNORECASE,
        )
        if col_match:
            col_name = col_match.group(1)
            col_type = col_match.group(2).upper()
            constraints = col_match.group(3).strip().rstrip(",") if col_match.group(3) else ""

            columns.append({
                "name": col_name,
                "type": col_type,
                "constraints": constraints,
                "comment": comment,
            })

    return columns


def parse_fts5_columns(body: str) -> list[dict]:
    """Parse FTS5 column definitions."""
    columns = []
    parts = [p.strip() for p in body.split(",")]

    for part in parts:
        # FTS5: column_name or column_name UNINDEXED
        match = re.match(r"(\w+)(?:\s+(UNINDEXED))?", part, re.IGNORECASE)
        if match:
            columns.append({
                "name": match.group(1),
                "type": "TEXT",
                "constraints": match.group(2) or "",
                "comment": "",
            })

    return columns


def generate_schema_docs() -> str:
    """Generate schema reference documentation."""
    schema_path = Path(__file__).parent.parent / "src" / "siftd" / "storage" / "schema.sql"
    sql = schema_path.read_text()

    lines = [
        "# Schema Reference",
        "",
        "_Auto-generated from `src/siftd/storage/schema.sql`._",
        "",
        "All primary keys are ULIDs (26-char TEXT, sortable by creation time).",
        "",
    ]

    sections = parse_schema(sql)

    for section in sections:
        lines.append(f"## {section['name']}")
        lines.append("")

        for table in section["tables"]:
            lines.append(f"### {table['name']}")
            lines.append("")

            if table.get("comment"):
                lines.append(table["comment"])
                lines.append("")

            if table.get("virtual"):
                lines.append(f"_Virtual table using {table['virtual']}._")
                lines.append("")

            if table["columns"]:
                lines.append("| Column | Type | Constraints | Notes |")
                lines.append("|--------|------|-------------|-------|")

                for col in table["columns"]:
                    constraints = escape_pipe(col["constraints"])
                    comment = escape_pipe(col["comment"])
                    lines.append(
                        f"| `{col['name']}` | {col['type']} | {constraints} | {comment} |"
                    )

                lines.append("")

    return "\n".join(lines)


# =============================================================================
# CLI Reference Generation
# =============================================================================


def run_help(args: list[str]) -> str:
    """Run siftd CLI with given args and capture help output."""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from siftd.cli import main as cli_main

    # Capture stdout/stderr from argparse --help
    stdout = io.StringIO()
    stderr = io.StringIO()

    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            cli_main(args)
    except SystemExit:
        pass  # argparse exits after --help

    return stdout.getvalue() or stderr.getvalue()


def public_commands() -> list[str]:
    """Enumerate public (non-plumbing) top-level commands via parser introspection.

    Never regex the rendered `--help` text: it moved from argparse's classic
    `{a,b,c}` brace to a custom lanes format once (silently truncating this
    generator to zero subcommands), and could again. `_LANES` is the CLI's own
    lane registry — the same source `siftd --help` renders from — and
    `test_every_command_is_laned_or_plumbing` (tests/cli/test_lane_grouping.py)
    already pins that every registered sub-command is either laned or in
    `_PLUMBING`, so this ordering is authoritative and exhaustive.
    """
    from siftd.cli import _LANES

    return [cmd for _lane, cmds in _LANES for cmd in cmds.split()]


def generate_cli_docs() -> str:
    """Generate CLI reference documentation."""
    lines = [
        "# CLI Reference",
        "",
        "_Auto-generated from `--help` output._",
        "",
    ]

    main_help = run_help(["--help"])
    lines.append("## siftd")
    lines.append("")
    lines.append("```")
    lines.append(main_help.strip())
    lines.append("```")
    lines.append("")

    for cmd in public_commands():
        cmd_help = run_help([cmd, "--help"])
        lines.append(f"## siftd {cmd}")
        lines.append("")
        lines.append("```")
        lines.append(cmd_help.strip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Config Reference Generation
# =============================================================================


def _toml_section(pattern: str) -> str:
    """Derive TOML section header from a schema pattern.

    Two-part keys (serve.host) → [serve].
    Three+-part keys (serve.auth.issuer) → [serve.auth].
    Wildcard sections (sync.remotes.*) keep the wildcard for display.
    """
    parts = pattern.split(".")
    if len(parts) <= 2:
        return parts[0]
    return ".".join(parts[:-1])


def generate_config_docs() -> str:
    """Generate config reference documentation from _CONFIG_SCHEMA."""
    from siftd.config import _CONFIG_SCHEMA

    lines = [
        "# Configuration Reference",
        "",
        "_Auto-generated from `src/siftd/config.py`._",
        "",
        "Config file: `~/.config/siftd/config.toml`",
        "",
        "All keys can be managed via `siftd config set <key> <value>`.",
        "",
    ]

    # Group by TOML section
    sections: dict[str, list] = {}
    for entry in _CONFIG_SCHEMA:
        section = _toml_section(entry.pattern)
        sections.setdefault(section, []).append(entry)

    for section, entries in sections.items():
        lines.append(f"## [{section}]")
        lines.append("")
        lines.append("| Key | Type | Default | Description |")
        lines.append("|-----|------|---------|-------------|")

        for entry in entries:
            # Show just the leaf key for readability
            leaf = entry.pattern.split(".")[-1]
            default = f"`{entry.default}`" if entry.default else "—"
            lines.append(
                f"| `{leaf}` | {entry.expected} | {default} | {escape_pipe(entry.description)} |"
            )

        lines.append("")

    # Add usage examples for non-obvious sections
    lines.extend([
        "## Examples",
        "",
        "```bash",
        "# Set static auth token",
        "siftd config set serve.auth.static_token mytoken123",
        "siftd config set serve.auth.identity kaygee",
        "",
        "# Configure OIDC",
        "siftd config set serve.auth.issuer https://your-idp.example.com",
        "",
        "# Override adapter discovery paths",
        "siftd config append adapters.claude_code.locations ~/.claude/projects",
        "",
        "# Disable update checks",
        "siftd config set update.check false",
        "```",
        "",
    ])

    return "\n".join(lines)


# =============================================================================
# README Generation (per-folder navigable docs)
# =============================================================================
#
# Managed READMEs carry hand-authored prose plus machine-generated spans bounded
# by `<!-- gen:begin <id> -->` / `<!-- gen:end -->`. Only content between markers
# is rewritten; authored prose is never touched. The manifest below declares
# which files are managed and which section each generated span renders. Every
# generated fact is derived from an authoritative source (module docstrings, the
# adapter registry, the doctor check registry) so the output is byte-stable
# across runs and Python versions.


@dataclasses.dataclass
class Section:
    """One generated span inside a managed README.

    Attributes:
        id: Marker id (`<!-- gen:begin <id> -->`); unique within its file.
        kind: Generator to run (modules, files, scripts, adapters,
            doctor-checks, tests).
        package: For `modules`/`files`: directory (relative to repo root) whose
            contents are enumerated. Ignored by other kinds.
    """

    id: str
    kind: str
    package: str | None = None


@dataclasses.dataclass
class ManagedReadme:
    """A README whose generated spans this tool owns.

    Attributes:
        path: File path relative to the repo root.
        summary: One-line description used only for the bootstrap placeholder
            preamble (real prose is authored in a later slice).
        sections: Generated spans, in declaration order.
    """

    path: str
    summary: str
    sections: list[Section]


# Checked-in manifest: explicit ownership only, no recursive README discovery.
# Summaries are lifted from the CLAUDE.md structure tree; they seed the
# bootstrap placeholder and are replaced by authored prose in a later slice.
MANIFEST: list[ManagedReadme] = [
    ManagedReadme(
        "src/siftd/README.md",
        "Core modules (config, search, pricing, ids, paths, safecall, …).",
        [Section("modules", "modules", "src/siftd")],
    ),
    ManagedReadme(
        "src/siftd/adapters/README.md",
        "Log parsing per tool (authoring SDK in adapters/sdk.py).",
        [Section("adapters", "adapters")],
    ),
    ManagedReadme(
        "src/siftd/api/README.md",
        "Public API layer — CLI and serve consume this, neither touches storage directly.",
        [Section("modules", "modules", "src/siftd/api")],
    ),
    ManagedReadme(
        "src/siftd/cli/README.md",
        "CLI package — thin dispatcher plus per-command modules.",
        [Section("modules", "modules", "src/siftd/cli")],
    ),
    ManagedReadme(
        "src/siftd/content/README.md",
        "Content-block helpers (binary filtering).",
        [Section("modules", "modules", "src/siftd/content")],
    ),
    ManagedReadme(
        "src/siftd/data/README.md",
        "Version-controlled reference data (pricing.toml).",
        [Section("files", "files", "src/siftd/data")],
    ),
    ManagedReadme(
        "src/siftd/doctor/README.md",
        "Health check system (per-check modules under doctor/checks/).",
        [Section("checks", "doctor-checks")],
    ),
    ManagedReadme(
        "src/siftd/domain/README.md",
        "Domain models (Conversation, Usage, events).",
        [Section("modules", "modules", "src/siftd/domain")],
    ),
    ManagedReadme(
        "src/siftd/embeddings/README.md",
        "Semantic search (optional [embed] extra).",
        [Section("modules", "modules", "src/siftd/embeddings")],
    ),
    ManagedReadme(
        "src/siftd/ingestion/README.md",
        "Ingest orchestration over adapters.",
        [Section("modules", "modules", "src/siftd/ingestion")],
    ),
    ManagedReadme(
        "src/siftd/output/README.md",
        "Format registry — terminal/markdown/json/html renderers.",
        [Section("modules", "modules", "src/siftd/output")],
    ),
    ManagedReadme(
        "src/siftd/peek/README.md",
        "Live session introspection (bypasses the DB).",
        [Section("modules", "modules", "src/siftd/peek")],
    ),
    ManagedReadme(
        "src/siftd/serialization/README.md",
        "Serve-layer JSON formatting (architecture boundary).",
        [Section("modules", "modules", "src/siftd/serialization")],
    ),
    ManagedReadme(
        "src/siftd/serve/README.md",
        "HTTP server (optional [serve] extra) — routes, auth, htmx UI.",
        [Section("modules", "modules", "src/siftd/serve")],
    ),
    ManagedReadme(
        "src/siftd/storage/README.md",
        "SQLite ops, schema, content blobs.",
        [Section("modules", "modules", "src/siftd/storage")],
    ),
    ManagedReadme(
        "tests/README.md",
        "Pytest suite — mirrors the src structure.",
        [Section("tests", "tests")],
    ),
    ManagedReadme(
        "scripts/README.md",
        "Dev harness commands, discovered as `./dev <name>`.",
        [Section("scripts", "scripts")],
    ),
]


# --- provenance + small extractors --------------------------------------------


def _provenance(source_desc: str) -> str:
    """One-line caption noting the generated span's source and regen command."""
    return f"<sub>generated from {source_desc} — run <code>./dev docs</code></sub>"


def _first_docstring_line(source: str) -> str:
    """First line of a Python module's docstring, via AST (no import)."""
    try:
        doc = ast.get_docstring(ast.parse(source))
    except SyntaxError:
        return ""
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _count_test_functions(source: str) -> int:
    """Count `test_*` functions/methods via AST (not grep)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def _first_comment_line(path: Path) -> str:
    """First `#`-comment line of a text file (e.g. a TOML header)."""
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
            if stripped:
                break
    except OSError:
        pass
    return ""


def _desc_line(path: Path) -> str:
    """The `# DESC:` header of a dev script."""
    for line in path.read_text().splitlines():
        if line.startswith("# DESC:"):
            return line[len("# DESC:") :].strip()
    return ""


# --- section renderers (each returns the span body: provenance + table) -------


def _render_modules(section: Section) -> str:
    assert section.package is not None
    pkg = REPO_ROOT / section.package
    py_files = sorted(p for p in pkg.glob("*.py") if p.name != "__init__.py")
    rows = []
    for p in py_files:
        summary = escape_pipe(_first_docstring_line(p.read_text())) or "—"
        rows.append(f"| [{p.name}]({p.name}) | {summary} |")
    if not rows:
        table = "_No modules._"
    else:
        table = "| Module | Summary |\n|--------|---------|\n" + "\n".join(rows)
    return _provenance("module docstrings") + "\n\n" + table


def _render_files(section: Section) -> str:
    assert section.package is not None
    directory = REPO_ROOT / section.package
    files = sorted(
        p for p in directory.iterdir() if p.is_file() and p.name != "README.md"
    )
    rows = []
    for p in files:
        desc = escape_pipe(_first_comment_line(p)) or "—"
        rows.append(f"| [{p.name}]({p.name}) | {desc} |")
    if not rows:
        table = "_No files._"
    else:
        table = "| File | Description |\n|------|-------------|\n" + "\n".join(rows)
    return _provenance("the data directory") + "\n\n" + table


def _render_scripts(_section: Section) -> str:
    # Mirror the ./dev discovery rules: scripts/*.sh, skip _*.sh (and, implicitly,
    # the lib/ examples/ prompts/ __pycache__ subdirs the glob never reaches).
    scripts = sorted(
        p for p in (REPO_ROOT / "scripts").glob("*.sh") if not p.name.startswith("_")
    )
    rows = []
    for p in scripts:
        desc = escape_pipe(_desc_line(p)) or "—"
        rows.append(f"| [{p.stem}]({p.name}) | {desc} |")
    table = "| Command | Description |\n|---------|-------------|\n" + "\n".join(rows)
    return _provenance("script DESC headers") + "\n\n" + table


def _render_adapters(_section: Section) -> str:
    from siftd.adapters.registry import load_builtin_adapters
    from siftd.adapters.validation import support_tier

    plugins = sorted(load_builtin_adapters(), key=lambda pl: pl.name)
    rows = []
    for pl in plugins:
        module = pl.module
        fname = Path(module.__file__).name
        tier = support_tier(module)
        doc = (module.__doc__ or "").strip()
        desc = escape_pipe(doc.splitlines()[0].strip()) if doc else "—"
        rows.append(f"| `{pl.name}` | [{fname}]({fname}) | {tier} | {desc} |")
    table = (
        "| Adapter | Module | Tier | Description |\n"
        "|---------|--------|------|-------------|\n" + "\n".join(rows)
    )
    return _provenance("the adapter registry") + "\n\n" + table


def _render_doctor_checks(_section: Section) -> str:
    from siftd.doctor.checks import BUILTIN_CHECKS

    checks = sorted(BUILTIN_CHECKS, key=lambda c: c.name)
    rows = []
    for check in checks:
        module = type(check).__module__  # e.g. siftd.doctor.checks.ingest_pending
        rel = module.split("siftd.doctor.", 1)[-1].replace(".", "/") + ".py"
        desc = escape_pipe(check.description) or "—"
        rows.append(f"| `{check.name}` | [{rel}]({rel}) | {check.cost} | {desc} |")
    table = (
        "| Check | Module | Cost | Description |\n"
        "|-------|--------|------|-------------|\n" + "\n".join(rows)
    )
    return _provenance("the doctor check registry") + "\n\n" + table


def _test_groups() -> list[tuple[str, list[tuple[str, int, str]]]]:
    """Group test files by directory: (label, [(relpath, count, summary), …])."""
    tests_dir = REPO_ROOT / "tests"

    def entries(paths: list[Path]) -> list[tuple[str, int, str]]:
        out = []
        for p in paths:
            source = p.read_text()
            rel = p.relative_to(tests_dir).as_posix()
            out.append((rel, _count_test_functions(source), _first_docstring_line(source)))
        return out

    groups: list[tuple[str, list[tuple[str, int, str]]]] = []
    root = sorted(tests_dir.glob("test_*.py"))
    if root:
        groups.append(("tests/", entries(root)))
    for sub in sorted(
        p for p in tests_dir.iterdir() if p.is_dir() and not p.name.startswith("__")
    ):
        files = sorted(sub.rglob("test_*.py"))
        if files:
            groups.append((f"tests/{sub.name}/", entries(files)))
    return groups


def _render_tests(_section: Section) -> str:
    groups = _test_groups()
    lines = [_provenance("test file docstrings"), "", "### Rollup", ""]
    lines.append("| Directory | Test files | Test functions |")
    lines.append("|-----------|------------|----------------|")
    for label, files in groups:
        n_funcs = sum(count for _, count, _ in files)
        lines.append(f"| `{label}` | {len(files)} | {n_funcs} |")
    for label, files in groups:
        lines.extend(["", f"### `{label}`", ""])
        lines.append("| File | Tests | Summary |")
        lines.append("|------|-------|---------|")
        for rel, count, summary in files:
            lines.append(f"| [{rel}]({rel}) | {count} | {escape_pipe(summary) or '—'} |")
    return "\n".join(lines)


_RENDERERS = {
    "modules": _render_modules,
    "files": _render_files,
    "scripts": _render_scripts,
    "adapters": _render_adapters,
    "doctor-checks": _render_doctor_checks,
    "tests": _render_tests,
}


def render_block(section: Section) -> str:
    """Render one section's span body (provenance caption + generated table)."""
    try:
        renderer = _RENDERERS[section.kind]
    except KeyError:
        raise ValueError(f"unknown section kind: {section.kind}") from None
    return renderer(section)


# --- marker engine ------------------------------------------------------------

_MARKER_RE = re.compile(r"<!-- gen:(begin|end)(?: (\S+))? -->")


def splice_markers(text: str, bodies: dict[str, str], *, source: str) -> str:
    """Rewrite the content between gen markers, leaving authored prose untouched.

    Idempotent: re-running with the same bodies yields identical text. Errors
    (via ValueError) on malformed, unclosed, nested, unknown, duplicate, or
    missing markers so drift and typos fail the build rather than degrading.

    Args:
        text: The full README text.
        bodies: Section id -> generated span body. Ids in the text must match
            this set exactly.
        source: Path label for error messages.

    Returns:
        The text with each marked span's interior replaced by its body.
    """
    blocks: list[tuple[int, int, str]] = []  # (inner_start, inner_end, id)
    open_id: str | None = None
    inner_start = 0
    seen: set[str] = set()

    for match in _MARKER_RE.finditer(text):
        kind, ident = match.group(1), match.group(2)
        if kind == "begin":
            if open_id is not None:
                raise ValueError(f"{source}: nested gen:begin ({ident!r} inside {open_id!r})")
            if ident is None:
                raise ValueError(f"{source}: gen:begin marker is missing an id")
            if ident in seen:
                raise ValueError(f"{source}: duplicate gen:begin id {ident!r}")
            if ident not in bodies:
                raise ValueError(f"{source}: unknown gen section id {ident!r}")
            seen.add(ident)
            open_id = ident
            inner_start = match.end()
        else:  # end
            if open_id is None:
                raise ValueError(f"{source}: gen:end without a matching gen:begin")
            blocks.append((inner_start, match.start(), open_id))
            open_id = None

    if open_id is not None:
        raise ValueError(f"{source}: unclosed gen:begin id {open_id!r}")
    missing = set(bodies) - seen
    if missing:
        raise ValueError(f"{source}: missing gen markers for {sorted(missing)}")

    out: list[str] = []
    pos = 0
    for start, end, ident in blocks:
        out.append(text[pos:start])
        out.append("\n" + bodies[ident] + "\n")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def fill_readme(entry: ManagedReadme, text: str) -> str:
    """Fill every generated span in one managed README's text."""
    bodies = {section.id: render_block(section) for section in entry.sections}
    return splice_markers(text, bodies, source=entry.path)


def _readme_title(entry: ManagedReadme) -> str:
    """H1 title for a managed README (e.g. `siftd.adapters`, `tests`)."""
    parent = Path(entry.path).parent
    if parent.name == "siftd":
        return "siftd"
    if parent.parts and parent.parts[0] == "src":
        return f"siftd.{parent.name}"
    return parent.name


def _bootstrap_shell(entry: ManagedReadme) -> str:
    """A minimal placeholder README: title, TODO preamble, empty gen spans."""
    lines = [
        f"# {_readme_title(entry)}",
        "",
        "<!-- TODO(preamble): authored in slice 3 -->",
        entry.summary,
        "",
    ]
    for section in entry.sections:
        lines.append(f"<!-- gen:begin {section.id} -->")
        lines.append("<!-- gen:end -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_readmes(*, bootstrap: bool = False) -> None:
    """Fill generated spans in every managed README.

    Missing files are a hard error unless ``bootstrap`` is set, in which case a
    placeholder shell is created first. Malformed markers raise ValueError.
    """
    for entry in MANIFEST:
        path = REPO_ROOT / entry.path
        if not path.exists():
            if not bootstrap:
                raise FileNotFoundError(
                    f"managed README missing: {entry.path} "
                    "(create it with: gen_docs.py readmes --bootstrap)"
                )
            path.write_text(_bootstrap_shell(entry))
        text = path.read_text()
        new_text = fill_readme(entry, text)
        if new_text != text:
            path.write_text(new_text)
            print(f"Generated: {path}")


# =============================================================================
# Main
# =============================================================================

DEFAULT_TARGETS = ["cli", "api", "schema", "config", "readmes"]


def run(targets: list[str], *, strict: bool = False, bootstrap: bool = False) -> int:
    """Generate the requested targets; return a process exit code.

    In strict mode a skipped target (api.md when optional deps are missing) is a
    hard failure so `./dev docs --check` cannot pass on an unverified doc.
    """
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []

    for target in targets:
        if target == "cli":
            out_path = DOCS_DIR / "cli.md"
            out_path.write_text(generate_cli_docs())
            print(f"Generated: {out_path}")

        elif target == "api":
            content = generate_api_docs()
            out_path = DOCS_DIR / "api.md"
            if content is None:
                skipped.append("api")
                print(f"Skipped: {out_path} (keeping existing version)")
            else:
                out_path.write_text(content)
                print(f"Generated: {out_path}")

        elif target == "schema":
            out_path = DOCS_DIR / "schema.md"
            out_path.write_text(generate_schema_docs())
            print(f"Generated: {out_path}")

        elif target == "config":
            out_path = DOCS_DIR / "config.md"
            out_path.write_text(generate_config_docs())
            print(f"Generated: {out_path}")

        elif target == "readmes":
            try:
                generate_readmes(bootstrap=bootstrap)
            except (FileNotFoundError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1

        else:
            print(f"Unknown target: {target}", file=sys.stderr)
            return 1

    if strict and skipped:
        print(
            f"error: strict mode — target(s) skipped, cannot verify: {', '.join(skipped)}",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> None:
    args = sys.argv[1:]
    strict = "--strict" in args
    bootstrap = "--bootstrap" in args
    list_only = "--list" in args
    targets = [a for a in args if not a.startswith("-")]

    if list_only:
        for entry in MANIFEST:
            print(entry.path)
        return

    if not targets:
        targets = DEFAULT_TARGETS
    sys.exit(run(targets, strict=strict, bootstrap=bootstrap))


if __name__ == "__main__":
    main()
