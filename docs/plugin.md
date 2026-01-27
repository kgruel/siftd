# Claude Code Plugin

The `plugin/` directory provides agent DX for strata inside Claude Code sessions. It teaches agents how to use strata effectively through hooks and a bundled skill.

## Prerequisites

strata must be installed and indexed separately — the plugin provides agent DX, not the CLI itself:

```bash
uv pip install /path/to/strata
strata ingest
strata ask --index
```

## Install

### From marketplace (recommended)

```bash
claude plugin marketplace add kaygee/strata
claude plugin install strata@strata
```

Scope options: `--scope user` (default, all projects), `--scope project` (shared via .claude/settings.json), `--scope local` (gitignored, personal).

### Dev mode

```bash
claude --plugin-dir plugin/
```

## What it does

The plugin has two components: **hooks** that nudge agents toward strata at the right moments, and a **skill** that teaches the search → refine → save workflow.

### Hooks

Three event hooks activate in different situations:

**Session start** — fires on session start (including after compaction/resume). Reminds agents that strata is available for researching past conversations.

**Skill reminder** — fires on prompt submit. Detects when the user mentions "strata" and nudges the agent to load the skill for workflow guidance.

**Skill required** — fires after Bash tool use. Detects when an agent runs raw `strata` commands without the skill loaded and suggests loading it for proper workflow context.

### Skill

The bundled skill (`plugin/skills/strata/SKILL.md`) uses progressive disclosure:

1. **Core** — basic search: `strata ask "query"`, `strata query <id>`
2. **Output** — reading modes: `--thread`, `--context`, `--chrono`
3. **Filtering** — narrowing results: `-w`, `-l`, `--since`, `--role`
4. **Preserving** — tagging workflow: `strata tag <id> research:topic`

Reference docs provide full flag coverage:
- `plugin/skills/strata/reference/ask.md` — all search flags
- `plugin/skills/strata/reference/query.md` — all query flags
- `plugin/skills/strata/reference/tags.md` — tag management

## Structure

```
plugin/
├── .claude-plugin/
│   └── plugin.json         # Plugin metadata (name, version, description)
├── hooks/
│   └── hooks.json          # Event hook definitions
├── scripts/
│   ├── session-start.sh    # SessionStart handler
│   ├── skill-reminder.sh   # UserPromptSubmit handler
│   └── skill-required.sh   # PostToolUse handler (Bash matcher)
└── skills/
    └── strata/
        ├── SKILL.md        # Progressive disclosure skill
        └── reference/
            ├── ask.md      # Full strata ask reference
            ├── query.md    # Full strata query reference
            └── tags.md     # Full tag management reference
```

## Customization

Copy built-in adapters or queries to your config directory for modification:

```bash
strata copy adapter claude_code    # ~/.config/strata/adapters/claude_code.py
strata copy query cost             # ~/.config/strata/queries/cost.sql
```

Same-name copies override built-in versions.
