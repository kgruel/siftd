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
   the generated adapter table). Register it where the existing adapters are
   registered (see `src/siftd/adapters/__init__.py`) with the appropriate
   support tier (new adapters start `contrib` unless the user says otherwise).
4. Generate a sanitized fixture with `./dev gen-adapter-fixture` (see the
   script's usage) and add parse tests under `tests/adapters/` mirroring an
   existing adapter's test file.
5. Run `./dev docs` (regenerates the adapter table + reference docs), then
   `./dev check`. Both must be green.
6. Dogfood if the tool's logs exist on this machine: `siftd ingest` in a
   scratch DB (`SIFTD_DATA_DIR=$(mktemp -d) uv run siftd ingest`) and confirm
   conversations appear.
