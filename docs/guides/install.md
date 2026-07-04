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

Semantic search needs an embedding backend, and there are two ways to get one — pick based on whether you want data to stay on your machine:

**Remote (no extra install)** — set a provider and API key in config, then build the index:

```bash
siftd config set embed.backend voyage
siftd config set embed.api_key env:VOYAGE_API_KEY
siftd embed
```

This sends conversation content to the configured provider (Voyage, OpenAI, Gemini, Jina, Mistral, or a custom OpenAI-compatible endpoint) at index and query time — see [Search](../concepts/search.md#privacy-when-does-data-leave-your-machine) for exactly when.

**Local (extra dependencies, no data leaves the machine)** — fastembed, numpy, onnxruntime:

```bash
siftd install embed
```

This detects how siftd was installed and runs the right command. Use `--dry-run` to preview.

A local Ollama server is a third option that needs no extra: set `embed.backend = ollama` and `embed.model` to a pulled embedding model (e.g. `nomic-embed-text`) — it's a remote-style client talking to `localhost`, so it uses the same no-extra base install as the hosted providers, but nothing leaves the machine.

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
siftd install embed             # add local semantic search (or configure a remote backend instead)
siftd embed                     # build the embeddings index
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
