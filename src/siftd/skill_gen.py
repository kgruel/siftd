"""Generate harness-appropriate instruction files from the bundled skill.

Each harness has its own convention for custom instructions. This module
renders the siftd skill content into the right format for each target.
"""

from __future__ import annotations

from pathlib import Path

# Harness-specific metadata
HARNESS_INFO: dict[str, dict] = {
    "claude_code": {
        "display_name": "Claude Code",
        "target_dir": "~/.claude/skills/siftd",
        "scope_dirs": {
            "user": "~/.claude/skills/siftd",
            "project": ".claude/skills/siftd",
        },
        "format": "skill",  # structured SKILL.md + reference/
    },
    "pi_agent": {
        "display_name": "Pi Agent",
        "target_dir": "~/.pi/agent/skills/siftd",
        "scope_dirs": {
            "user": "~/.pi/agent/skills/siftd",
        },
        "format": "skill",  # same structure as Claude Code
    },
    "codex_cli": {
        "display_name": "Codex CLI",
        "target_dir": "~/.codex",
        "filename": "siftd.md",
        "scope_dirs": {
            "user": "~/.codex",
        },
        "format": "instructions",  # plain markdown, appended or standalone
    },
    "gemini_cli": {
        "display_name": "Gemini CLI",
        "target_dir": "~/.gemini",
        "filename": "siftd.md",
        "scope_dirs": {
            "user": "~/.gemini",
        },
        "format": "instructions",
    },
    "copilot_cli": {
        "display_name": "Copilot CLI",
        "target_dir": ".github",
        "filename": "siftd-instructions.md",
        "scope_dirs": {
            "project": ".github",
        },
        "format": "instructions",
    },
    "aider": {
        "display_name": "Aider",
        "target_dir": ".",
        "filename": ".aider.siftd.md",
        "scope_dirs": {
            "project": ".",
        },
        "format": "instructions",
    },
}


def render_instructions(reference_dir: Path) -> str:
    """Render a plain-markdown instructions file from reference docs.

    Strips Claude Code-specific frontmatter and skill invocation patterns.
    Produces a generic instruction file any LLM agent can follow.
    """
    lines = [
        "# siftd — Search past coding conversations",
        "",
        "siftd is installed on this system. Use it to search past coding conversations,",
        "find decisions, trace how ideas evolved, and retrieve context.",
        "",
        "## Quick reference",
        "",
        "```bash",
        "# Search",
        'siftd search "query" --thread          # narrative results',
        'siftd search -w project "query"        # scoped to workspace',
        'siftd search --first "concept"         # earliest mention',
        "",
        "# Browse",
        "siftd query                            # recent conversations",
        "siftd query <id>                       # drill into one",
        "siftd query -w project                 # filter by workspace",
        "",
        "# Tag",
        "siftd tag <id> research:topic          # bookmark for later",
        "siftd tag --last decision:auth         # tag most recent",
        "siftd query -l research:topic          # retrieve by tag",
        "",
        "# Live sessions",
        "siftd peek                             # view active sessions",
        "siftd peek <id> --last-response        # extract last response",
        "```",
        "",
        "## Tag conventions",
        "",
        "| Prefix | Usage |",
        "|--------|-------|",
        "| `decision:*` | Architectural/design decisions |",
        "| `research:*` | Investigation findings |",
        "| `useful:*` | Reusable patterns/examples |",
        "| `rationale:*` | Why X over Y |",
        "| `genesis:*` | First discussion of a concept |",
        "",
        "## Research workflow",
        "",
        "1. Search broadly, then narrow with `-w <workspace>` or `--since`",
        "2. Drill into results with `siftd query <id>`",
        "3. Tag useful findings: `siftd tag <id> research:<topic>`",
        "4. Future retrieval: `siftd query -l <tag>` or `siftd search -l <tag> \"query\"`",
        "",
    ]

    # Append reference docs if available
    for ref_name in ("search", "query", "tags"):
        ref_file = reference_dir / f"{ref_name}.md"
        if ref_file.exists():
            lines.append(f"## {ref_name.title()} — full reference")
            lines.append("")
            # Strip any leading # header from the reference doc
            ref_text = ref_file.read_text().strip()
            for line in ref_text.splitlines():
                if line.startswith("# "):
                    continue  # skip top-level header (redundant)
                lines.append(line)
            lines.append("")

    return "\n".join(lines)
