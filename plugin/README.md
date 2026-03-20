# siftd plugin for Claude Code

Gives Claude Code agents access to your conversation history. Provides a single skill for semantic search, query, and tagging, plus hooks that guide the research workflow.

## Prerequisites

siftd must be installed and indexed:

```bash
uv pip install /path/to/siftd   # or pip install .
siftd ingest                     # ingest conversation logs
siftd search --index             # build embeddings index
```

## Install

### From marketplace (recommended)

```bash
# Add the siftd marketplace
claude plugin marketplace add kgruel/siftd

# Install the plugin
claude plugin install siftd@siftd
```

Scope options:

```bash
claude plugin install siftd@siftd                # user (default) — all projects
claude plugin install siftd@siftd --scope project # project — shared via .claude/settings.json
claude plugin install siftd@siftd --scope local   # local — gitignored, personal only
```

Or interactively: run `/plugin` in Claude Code, navigate to **Discover**, select siftd, and choose your scope.

### Dev mode (for development)

```bash
claude --plugin-dir /path/to/siftd/plugin/
```

## What it provides

### Skill: `siftd`

The `/siftd` skill teaches agents the research workflow: search past conversations, drill down into results, and tag findings for later retrieval.

Reference docs cover `search`, `query`, and `tags` commands with all flags and composition patterns.

### Commands

Direct-execution commands for manual workflows:

| Command | Description |
|---------|-------------|
| `/siftd:search "query"` | Run search and see raw output |
| `/siftd:query [id]` | List conversations or drill into one |
| `/siftd:peek [id]` | View live/recent sessions (bypasses DB) |
| `/siftd:tag <tag>` | Tag current session or conversation |

Commands run siftd directly and show output without agent interpretation. Use these when you want to drive the workflow yourself.

### Hooks

Four hooks support the research workflow:

| Hook | Trigger | Behavior |
|------|---------|----------|
| `SessionStart` | Session start/resume/compact | Registers session for live tagging; reminds agent on compact/resume |
| `Stop` | Session exit | Runs `siftd ingest -a claude_code` to apply pending tags |
| `UserPromptSubmit` | User mentions "siftd" or past sessions | Suggests loading the skill (high-precision patterns only) |
| `PostToolUse` | Agent runs `siftd` in Bash | Contextual tips, once per subcommand per session |

## Structure

```
plugin/
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── hooks/
│   └── hooks.json        # Hook definitions
├── scripts/
│   ├── session-start.sh  # Register session + remind on compact/resume
│   ├── stop.sh           # Auto-ingest on session exit (applies pending tags)
│   ├── skill-reminder.sh # Detect research intent in user prompts
│   └── post-siftd.sh     # Contextual tips after siftd commands
├── commands/
│   ├── siftd:search.md   # Direct search execution
│   ├── siftd:query.md    # List/drill-down conversations
│   ├── siftd:peek.md     # Live session inspection
│   └── siftd:tag.md      # Tag with session detection feedback
└── skills/
    └── siftd/
        ├── SKILL.md
        └── reference/
            ├── search.md
            ├── query.md
            └── tags.md
```

## Updating

```bash
# Update the marketplace (pulls latest plugin versions)
claude plugin marketplace update siftd
```

Or in Claude Code: `/plugin` → marketplace tab → update.
