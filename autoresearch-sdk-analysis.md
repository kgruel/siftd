# SDK ↔ Adapter Gap Analysis

## The problem

The SDK (`sdk.py`) provides helpers that only 3 of 8 adapters actually use well.
The other 5 reimplement the same patterns because the SDK's abstractions don't fit
their record schemas. This means:

1. **Lots of copy-paste code** — discover, peek_scan, peek_exchanges are reimplemented
   with minor variations
2. **Tests need per-adapter builders** — because each adapter has its own record format,
   the test infrastructure mirrors the duplication
3. **The template adapter shows the ideal** but most real adapters can't use it

## Current SDK adoption

| Adapter      | discover | peek_scan | peek_exchanges | load_jsonl | Notes |
|-------------|----------|-----------|---------------|-----------|-------|
| pi_agent     | ✅ sdk   | — (none)  | — (none)       | ✅ sdk    | Good citizen |
| copilot_cli  | ✅ sdk   | — (none)  | — (none)       | ✅ sdk    | Good citizen |
| vscode       | ✅ sdk   | — (none)  | — (none)       | —         | Good citizen |
| claude_code  | ❌ custom | ❌ custom | ✅ sdk         | ✅ _jsonl | Partial — peek_scan needs subagent logic |
| codex_cli    | ❌ custom | ❌ custom | ❌ custom      | ✅ _jsonl | Full reimplementation |
| gemini_cli   | ❌ custom | ❌ custom | ❌ custom      | —         | JSON not JSONL (legitimately different) |
| aider        | ❌ custom | — (none)  | — (none)       | —         | Markdown (legitimately different) |
| opencode     | ❌ custom | — (none)  | — (none)       | —         | SQLite (legitimately different) |

## The three tiers

### Tier 1: Good SDK citizens (pi_agent, copilot_cli, vscode)
Use `discover_files()`, `load_jsonl()`, `build_harness()`. These are the model.

### Tier 2: Should use SDK but don't (claude_code, codex_cli)
Both are JSONL-based. Both have `discover()` that's identical to `sdk.discover_files()`.
Both have `peek_scan()` that reimplements the same "iterate lines, parse JSON, track
timestamps, count exchanges" loop.

**Why they don't use the SDK:**
- `peek_jsonl_scan` assumes top-level `type: "user"/"assistant"` records
- Codex uses `type: "response_item"` with `payload.type: "message", payload.role: "user"`
- Codex has `session_meta` and `turn_context` record types for metadata
- Claude Code needs subagent detection (`agentId` field, `/subagents/` path)

### Tier 3: Legitimately different (gemini_cli, aider, opencode)
Different storage formats entirely (JSON, markdown, SQLite). Custom code is appropriate.

## What's duplicated

### 1. `discover()` — 3 identical copies

```python
# claude_code.py, codex_cli.py — exact same pattern
def discover(locations=None):
    for location in (locations or DEFAULT_LOCATIONS):
        base = Path(location).expanduser()
        if not base.exists():
            continue
        for jsonl_file in base.glob("**/*.jsonl"):
            yield Source(kind="file", location=jsonl_file)
```

vs what pi_agent does:
```python
def discover(locations=None):
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/*.jsonl"])
```

**Fix**: claude_code and codex_cli should use `discover_files()`. Zero behavior change.

### 2. `peek_scan()` — the "iterate JSONL and extract metadata" pattern

Three implementations (sdk generic, claude_code custom, codex_cli custom) that all:
1. Open file, iterate lines
2. Parse JSON, skip errors
3. Track timestamp bounds (started_at, last_activity_at)
4. Count exchanges (user→assistant pairs)
5. Extract metadata (session_id, workspace, model)
6. Return PeekScanResult

The difference is **where the metadata lives in each record**:
- Claude: `record["type"]`, `record["cwd"]`, `record["sessionId"]`, `record["message"]["model"]`
- Codex: `record["type"]` → "session_meta"/"response_item", metadata in `record["payload"]`

### 3. `peek_exchanges()` — codex reimplements 88 lines

Claude Code delegates to `sdk.peek_jsonl_exchanges()` by providing callbacks:
```python
get_content_blocks=_get_content_blocks,
get_usage=_get_usage,
is_tool_result=_is_tool_result,
```

Codex can't do this because:
- Content blocks are at `payload.content` not `message.content`
- Usage comes from separate `event_msg` records, not inline
- Tool calls come from `function_call`/`custom_tool_call` payload types

### 4. `can_handle()` — repetitive path-checking logic

Every adapter does the same pattern: check `source.kind`, check file extension,
check if path is under DEFAULT_LOCATIONS or has a marker directory.

## The deeper issue

`sdk.peek_jsonl_scan` tries to be generic through **parameter configuration**:
```python
peek_jsonl_scan(path, user_type="user", assistant_type="assistant",
                type_key="type", cwd_key="cwd", ...)
```

But Codex's schema isn't just "different key names" — it's a different **record structure**.
Codex wraps everything in `response_item` with `payload.type` dispatch. You can't configure
your way through a structural difference.

## Proposed direction: Record normalization

Instead of making `peek_jsonl_scan` accept more parameters, have each adapter provide
a **record normalizer** — a function that maps its native records to a common intermediate:

```python
@dataclass
class NormalizedRecord:
    kind: str          # "user" | "assistant" | "metadata" | "tool_use" | "tool_result" | "usage"
    timestamp: str | None
    content_blocks: list[dict]    # for user/assistant
    metadata: dict                # for metadata (session_id, workspace, model)
    usage: tuple[int, int] | None # for usage
```

Each adapter provides:
```python
def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map native record to normalized form."""
```

Then the SDK can provide:
```python
def peek_scan(path, normalize_record) -> PeekScanResult | None
def peek_exchanges(path, normalize_record, last_n) -> list[PeekExchange]
```

This means:
- SDK owns the "iterate, count, track timestamps" logic (one implementation)
- Adapters own "what does MY record format look like" (small, testable function)
- Test builders generate records and call normalizer — one builder class, different normalizers

## What about `parse()`?

The full `parse()` functions are more complex (tool call linking, pending calls, etc.)
but they share the same skeleton:
1. Load records
2. Extract metadata (session_id, workspace, model, timestamps)
3. Create Conversation + Harness
4. Iterate records: user → Prompt, assistant → Response, tool_use → pending, tool_result → resolve
5. Finalize pending tool calls
6. Yield conversation

The `ToolCallLinker` in sdk.py already addresses step 4-5 but only claude_code's
template uses it. A `parse_jsonl_conversation()` SDK function could handle the full
skeleton if given a record normalizer.

## Recommended refactors (ordered by impact)

### Phase 1: Low-hanging fruit (no new abstractions) — DONE
1. ~~**claude_code, codex_cli**: use `discover_files()` instead of custom discover~~
2. ~~**All 7 non-vscode adapters**: use `build_harness()` instead of inline Harness()~~
3. ~~**4 adapters**: use `flush_pending_calls()` SDK helper~~

### Phase 2: Record normalization (new abstraction) — DONE
4. ~~Add `NormalizedRecord` + `normalize_record` callback pattern to SDK~~
5. ~~Add `peek_scan_from_records` / `peek_exchanges_from_records` (format-agnostic)~~
6. ~~Provide normalizers for all 4 JSONL adapters (claude_code, codex_cli, pi_agent, copilot_cli)~~
7. ~~pi_agent, copilot_cli gain peek support for free via `make_peek_hooks`~~
8. ~~claude_code, codex_cli peek migrated to `make_peek_hooks` (eliminated ~345 lines)~~
9. ~~Subagent detection promoted to SDK (`extra["agent_id"]` + `SUBAGENT_PATH_MARKER`)~~
10. ~~Peek scanner/reader auto-derive hooks from `normalize_record`~~

### Phase 2.5: Test builder unification (next)
11. One `SessionBuilder` parameterized by normalizer/format
12. Builder generates normalized records, format object serializes to native format
13. Autoresearch can write format-agnostic integration tests

### Phase 3: Parse skeleton (stretch goal)
14. Extract common parse skeleton into SDK
15. Adapters provide record normalizer + metadata extractor
16. Only truly custom parse logic (subagents, SQLite, markdown) stays in adapters

### Phase 4: Non-JSONL peek (opportunistic)
17. Add normalizers for vscode (JSON), gemini_cli (JSON — already has custom peek)
18. Add normalizers for aider (markdown), opencode (SQLite) — needs custom record iterators

## Impact on test builders

Today: `ClaudeSession`, `CodexSession`, `PeekSession` — three builders, three formats.

After Phase 2.5: One `SessionBuilder` parameterized by normalizer/format:
```python
session = SessionBuilder(tmp_path, format=CodexFormat(), exchanges=2).with_tools(["shell"]).build()
```

Or even simpler — the builder generates normalized records, and the format object
serializes them to the adapter's native format.

## What was eliminated (branch: refactor/sdk-adapter-consolidation)

| Change | Lines removed | Lines added |
|--------|-------------|------------|
| discover_files() swap | -18 | +2 |
| NormalizedRecord + SDK peek infrastructure | — | +350 |
| pi_agent, copilot_cli normalizers + peek | — | +115 |
| claude_code, codex_cli normalizers | — | +130 |
| claude_code peek → make_peek_hooks | -160 | +3 |
| codex_cli peek → make_peek_hooks | -185 | +3 |
| build_harness() across 7 adapters | -28 | +7 |
| flush_pending_calls across 4 adapters | -40 | +4 |
| **Total** | **-431** | **+614** |

Net +183 lines, but the SDK gained ~500 lines of reusable infrastructure that
5 adapters now share. The per-adapter surface area dropped significantly.
