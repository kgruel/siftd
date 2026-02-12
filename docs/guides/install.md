# Installation

## Installing siftd

The CLI tool. Pick one method:

```bash
# Recommended — isolated environment, auto-managed
uv tool install siftd

# Alternative isolated environment
pipx install siftd

# Into an existing venv or system Python
pip install siftd

# macOS via Homebrew
brew tap kgruel/siftd
brew install siftd
```

### Optional: embeddings

Semantic search requires extra dependencies (fastembed, numpy, onnxruntime):

```bash
siftd install embed
```

This detects how siftd was installed and runs the right command. Use `--dry-run` to preview.

## Installing the Claude Code plugin

The plugin adds hooks, commands, and skills to Claude Code for tagging and searching past sessions.

### Method 1: Bundled (recommended)

The plugin ships inside the siftd wheel. Install it to your Claude Code plugins directory:

```bash
# User-scope (default) — all projects
siftd install plugin

# Project-scope — current project only
siftd install plugin --scope project
```

Use `--dry-run` to see source and target paths without writing.

### Method 2: Marketplace

```bash
claude plugin install siftd
```

### Method 3: Dev mode

Point Claude Code at the plugin directory directly:

```bash
claude --plugin-dir /path/to/siftd/plugin
```

Useful during plugin development.

## Getting started

After installing:

```bash
siftd ingest                    # import conversation logs
siftd query                     # list recent conversations
siftd install embed             # add semantic search
siftd search --index            # build embeddings index
siftd search "your query"       # search
```

## Updating

| Install method | Update command |
|---|---|
| `uv tool` | `uv tool upgrade siftd` |
| `pipx` | `pipx upgrade siftd` |
| `pip` | `pip install --upgrade siftd` |
| Homebrew | `brew update && brew upgrade siftd` |

After upgrading siftd, re-run `siftd install plugin` to update the bundled plugin files.
