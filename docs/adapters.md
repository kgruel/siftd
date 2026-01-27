# Adapters

Adapters are the parsing boundary between raw log formats and strata's normalized data model. Each adapter knows how to discover log files, parse them into domain objects, and manage deduplication.

## Built-in adapters

| Adapter | Tool | Format | Dedup strategy | Default location |
|---------|------|--------|---------------|------------------|
| `claude_code` | Claude Code | JSONL | File (one conversation per file) | `~/.claude/projects/` |
| `gemini_cli` | Gemini CLI | JSON array | Session (latest wins) | `~/.gemini/` |
| `codex_cli` | Codex CLI | JSONL | File | `~/.codex/` |
| `aider` | Aider | Markdown | File | `~/.aider/` |

List discovered adapters (built-in, drop-in, and entry point):

```bash
strata adapters
```

## Dedup strategies

Adapters declare how deduplication works for their log format:

**File dedup** (`claude_code`, `codex_cli`, `aider`): Each file maps to one conversation. If the file hash changes, the old conversation is deleted and re-ingested. If the hash matches, skip.

**Session dedup** (`gemini_cli`): A single file may contain multiple sessions. Each session has an external ID. If a newer version of the same session is found, it replaces the old one.

## Writing a drop-in adapter

Place a Python file in `~/.config/strata/adapters/`:

```
~/.config/strata/adapters/my_tool.py
```

The module must define these attributes and functions:

```python
from strata.domain.models import Conversation
from strata.domain.source import Source

# Required attributes
NAME = "my_tool"                          # Unique adapter name
DEFAULT_LOCATIONS = ["~/.my_tool/logs"]   # Where to find log files
DEDUP_STRATEGY = "file"                   # "file" or "session"
HARNESS_SOURCE = "openai"                 # Provider name for this tool
HARNESS_LOG_FORMAT = "jsonl"              # Log format identifier

# Optional: tool name mapping (raw name → canonical name)
TOOL_ALIASES = {
    "RawToolName": "file.read",
    "execute_command": "shell.execute",
}

def discover(locations=None) -> list[Source]:
    """Find all log files this adapter can handle.

    If locations is None, use DEFAULT_LOCATIONS.
    Return Source objects for each discovered file.
    """

def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""

def parse(source: Source) -> list[Conversation]:
    """Parse a source into Conversation domain objects.

    Each Conversation contains Prompts, Responses, and ToolCalls
    as a nested dataclass tree.
    """
```

### Overriding built-in adapters

A drop-in adapter with the same `NAME` as a built-in will override it. This lets you customize parsing for a tool without forking strata.

Copy a built-in adapter as a starting point:

```bash
strata copy adapter claude_code
# Creates ~/.config/strata/adapters/claude_code.py
```

### Adapter validation

Adapters are validated at load time. Missing required attributes raise an error. Use `strata doctor` to check that drop-in adapters load correctly — the `drop-ins-valid` check verifies all adapters in the config directory.

## Entry point adapters

For packaged adapters, use the `strata.adapters` entry point group:

```toml
# In your package's pyproject.toml
[project.entry-points."strata.adapters"]
my_tool = "my_package.strata_adapter"
```

The module interface is the same as drop-in adapters.

## Discovery order

1. **Built-in** adapters (shipped with strata)
2. **Entry point** adapters (installed packages)
3. **Drop-in** adapters (`~/.config/strata/adapters/*.py`)

Later entries override earlier ones by `NAME`.

## Reference implementation

See `src/strata/adapters/claude_code.py` for a complete example of a production adapter with tool aliases, cache token extraction, and file-based dedup.
