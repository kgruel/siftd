# siftd plugin for Claude Code

Gives Claude Code agents access to your conversation history. Provides skills for semantic search, query, and tagging, plus hooks that nudge agents toward using the skill workflow.

## Prerequisites

siftd must be installed and indexed:

```bash
uv pip install /path/to/siftd   # or pip install .
siftd ingest                     # ingest conversation logs
siftd ask --index                # build embeddings index
```

## Install

### From marketplace (recommended)

```bash
# Add the siftd marketplace
claude plugin marketplace add kaygee/siftd

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

Reference docs cover `ask`, `query`, and `tags` commands with all flags and composition patterns.

### Hooks

Three hooks nudge agents toward the skill:

| Hook | Trigger | Behavior |
|------|---------|----------|
| `SessionStart` | Session start/resume | Reminds agent that siftd is available |
| `UserPromptSubmit` | User mentions "siftd" | Suggests loading the skill |
| `PostToolUse` | Agent runs `siftd` in Bash | Nudges toward using the Skill tool instead |

## Structure

```
plugin/
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── hooks/
│   └── hooks.json        # Hook definitions
├── scripts/
│   ├── session-start.sh
│   ├── skill-reminder.sh
│   └── skill-required.sh
└── skills/
    └── siftd/
        ├── SKILL.md
        └── reference/
            ├── ask.md
            ├── query.md
            └── tags.md
```

## Updating

```bash
# Update the marketplace (pulls latest plugin versions)
claude plugin marketplace update siftd
```

Or in Claude Code: `/plugin` → marketplace tab → update.
