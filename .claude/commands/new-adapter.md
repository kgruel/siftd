---
description: Scaffold a new siftd log adapter (module, fixture, tests, tier registration, docs)
argument-hint: <tool-name> [log-path-hint]
---

Create a new siftd adapter for: $ARGUMENTS

Follow the full workflow — do not skip steps:

1. Read `docs/guides/writing-adapters.md` and `src/siftd/adapters/README.md`
   (tier semantics), plus one existing adapter of similar shape (JSONL log →
   `src/siftd/adapters/claude_code.py`; sqlite DB → `opencode.py`; directory
   sessions → `codex_cli.py`) and `src/siftd/adapters/sdk.py`.
2. Inspect the actual log format on disk first (the path hint above, or ask).
   Never guess field names — parse a real sample.
3. Implement `src/siftd/adapters/<name>.py`: `can_handle()`, `parse()`,
   `discover()`, `ADAPTER_INTERFACE_VERSION = 1`, module docstring (it feeds
   the generated adapter table). Register it in BOTH places: the import/export
   in `src/siftd/adapters/__init__.py` AND the explicit builtin list in
   `src/siftd/adapters/registry.py` (`load_builtin_adapters()`) — missing the
   second leaves the adapter undiscovered by ingest and the generated docs.
   Set the appropriate support tier (new adapters start `contrib` unless the
   user says otherwise).
4. Generate a sanitized fixture with `./dev gen-adapter-fixture` (see the
   script's usage) and add parse tests under `tests/adapters/` mirroring an
   existing adapter's test file.
5. Run `./dev docs` (regenerates the adapter table + reference docs), then
   `./dev check`. Both must be green.
6. Dogfood if the tool's logs exist on this machine: `siftd ingest` into a
   scratch DB and confirm conversations appear. Isolation is via XDG
   (`siftd.paths` reads `XDG_DATA_HOME`, there is no SIFTD_* override):
   `XDG_DATA_HOME=$(mktemp -d) uv run siftd ingest`.
