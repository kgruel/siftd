#!/usr/bin/env python3
"""Generate reference documentation from code.

Usage:
    python scripts/gen_docs.py          # Generate all docs
    python scripts/gen_docs.py api      # Generate API reference only
    python scripts/gen_docs.py schema   # Generate schema reference only
    python scripts/gen_docs.py cli      # Generate CLI reference only
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
from pathlib import Path
from typing import Any, get_type_hints

# Ensure src is importable when running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

DOCS_DIR = Path(__file__).parent.parent / "docs" / "reference"


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
    return str(t).replace("typing.", "")


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


def generate_api_docs() -> str:
    """Generate API reference documentation."""
    from siftd import api

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
        obj = getattr(api, name)
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
            obj = getattr(api, name)
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


def extract_subcommands(help_text: str) -> list[str]:
    """Extract subcommand names from main help output."""
    # Look for the {cmd1,cmd2,...} pattern in usage line
    match = re.search(r"\{([^}]+)\}", help_text)
    if match:
        return [cmd.strip() for cmd in match.group(1).split(",")]
    return []


def generate_cli_docs() -> str:
    """Generate CLI reference documentation."""
    lines = [
        "# CLI Reference",
        "",
        "_Auto-generated from `--help` output._",
        "",
    ]

    # Get main help
    main_help = run_help(["--help"])
    subcommands = extract_subcommands(main_help)

    lines.append("## siftd")
    lines.append("")
    lines.append("```")
    lines.append(main_help.strip())
    lines.append("```")
    lines.append("")

    # Get help for each subcommand
    for cmd in subcommands:
        cmd_help = run_help([cmd, "--help"])
        lines.append(f"## siftd {cmd}")
        lines.append("")
        lines.append("```")
        lines.append(cmd_help.strip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["cli", "api", "schema"]

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    for target in targets:
        if target == "cli":
            content = generate_cli_docs()
            out_path = DOCS_DIR / "cli.md"
            out_path.write_text(content)
            print(f"Generated: {out_path}")

        elif target == "api":
            content = generate_api_docs()
            out_path = DOCS_DIR / "api.md"
            out_path.write_text(content)
            print(f"Generated: {out_path}")

        elif target == "schema":
            content = generate_schema_docs()
            out_path = DOCS_DIR / "schema.md"
            out_path.write_text(content)
            print(f"Generated: {out_path}")

        else:
            print(f"Unknown target: {target}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
