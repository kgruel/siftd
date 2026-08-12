# siftd.adapters

Each module here is the translation layer for one coding tool: it reads that tool's raw log format and produces siftd's domain `Conversation` objects. Adapters own parsing only — they never touch the database or the storage layer, and they never depend on `api/`, `cli/`, or `serve/`. Shared authoring helpers (file discovery, JSONL loading, tool-call linking, peek scaffolding) live in [sdk.py](sdk.py); the interface contract is defined and enforced in [validation.py](validation.py); [template.py](template.py) is the blank starting point copied by `siftd copy adapter template`.

The interface is a set of module-level attributes, not a base class. Every adapter must export `ADAPTER_INTERFACE_VERSION = 1`, `NAME`, `DEFAULT_LOCATIONS`, `DEDUP_STRATEGY` (`"file"` or `"session"`), and `HARNESS_SOURCE`, plus the callables `discover()`, `can_handle()`, and `parse()`. `SUPPORT_TIER` is optional and defaults to `"contrib"` — the `core`/`contrib`/`frozen` tier sets format-tracking expectations, shows up in `siftd adapters`, and scopes parse-error warnings for non-core adapters. Two invariants are easy to break. The first is per-strategy: a `"file"` adapter's `parse()` may yield at most one conversation per source (ingestion fails the source otherwise, and `tests/architecture/test_dedup_cardinality.py` fails the fixture first), while a `"session"` adapter's source is a container and may yield many — pick `"session"` whenever one source grows new conversations over its life, whatever its storage format. The second: raw tool names must be mapped to canonical `category.action` forms via `TOOL_ALIASES` so cross-tool queries work. The registry is not hardcoded — built-ins are imported here, but drop-in adapters in `~/.config/siftd/adapters/` and entry-point packages are auto-discovered, so add capability through the interface rather than a central list.

See [Adapters](../../../docs/concepts/adapters.md) for the conceptual model and tier semantics, and [Writing Adapters](../../../docs/guides/writing-adapters.md) for the full implementation guide including optional peek hooks.

<!-- gen:begin adapters -->
<sub>generated from the adapter registry — run <code>./dev docs</code></sub>

| Adapter | Module | Tier | Description |
|---------|--------|------|-------------|
| `aider` | [aider.py](aider.py) | frozen | Aider adapter for siftd. |
| `antigravity_cli` | [antigravity_cli.py](antigravity_cli.py) | core | Antigravity CLI adapter for siftd. |
| `claude_code` | [claude_code.py](claude_code.py) | core | Claude Code adapter for siftd. |
| `codex_cli` | [codex_cli.py](codex_cli.py) | core | Codex CLI adapter for siftd. |
| `copilot_cli` | [copilot_cli.py](copilot_cli.py) | contrib | Copilot CLI adapter for siftd. |
| `gemini_cli` | [gemini_cli.py](gemini_cli.py) | frozen | Gemini CLI adapter for siftd. |
| `opencode` | [opencode.py](opencode.py) | contrib | OpenCode adapter for siftd. |
| `pi_agent` | [pi_agent.py](pi_agent.py) | contrib | Pi Coding Agent adapter for siftd. |
| `vscode` | [vscode.py](vscode.py) | contrib | VSCode chat adapter for siftd. |
<!-- gen:end -->
