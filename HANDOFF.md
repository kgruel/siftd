# siftd — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and semantic search.

## Current Focus

**CLI Quality** — Post-decomposition cleanup from code review.

Next: deferred review items (branch `review-fixes` or new branch):
- [ ] **NULL workspace_path asymmetry** in `find_active_session` — document as intentional
- [ ] **Double `parse_date` calls** — argparse `type=` + manual call. Idempotent but redundant
- [ ] **Break down `cmd_search()`** — 367 lines, works but long
- [ ] **Architecture test gaps** — doesn't detect raw SQL in CLI modules or imports outside 4 declared layers

Done this session:
- [x] Fixed connection leak in `_search_fts_only` (try/finally)
- [x] Fixed `open_database` import consistency in `api/search.py`
- [x] Added API wrappers: `list_workspaces`, `resolve_entity_id`, `get_recent_conversation_ids`
- [x] Eliminated all KNOWN_VIOLATIONS (cli→storage layer breaches)
- [x] Extracted `cli_peek.py` and `cli_export.py` — cli.py now 59 lines (pure dispatcher)
- [x] Added `resolve_db(args)` helper, replaced 12 occurrences across 7 modules
- [x] Lazy imports in `cli_data.py` for adapters, backfill, ingestion

Previous session:
- [x] CLI decomposition: extracted cli_meta, cli_tags, cli_query, cli_data, cli_sessions, cli_common
- [x] Agent ergonomics: session-id fallback, FTS5 warnings, workspaces cmd

Previous session:
- [x] Merged `impl/worktree-identity` — branch display in peek, `--branch` filter
- [x] Agent monitoring patterns, config philosophy research

Previous session:
- [x] Fixed CI failures (broken since Feb 1 when git worktree tests were added)

## Friction Log

Discovered via "siftd monitoring siftd" pattern — using siftd to observe agent workflows.

| Issue | Type | Status |
|-------|------|--------|
| ~~Extracting last response requires raw jq, not peek~~ | UX | Fixed (`--last-response`) |
| peek/query show different data (live files vs DB) | Conceptual | Documented |
| ~~Peek session ID ambiguity (same prefix, multiple files)~~ | Bug | Fixed (working as designed) |
| ~~Peek read failures (CLI fails, Python adapter works)~~ | Bug | Fixed (`can_handle()` location-aware) |
| ~~Peek slow (12s) for session lookup~~ | Perf | Fixed (path-based filtering) |
| ~~Workspace resolution assigns wrong workspace (worktree)~~ | Bug | Fixed |
| ~~`query <id>` output too verbose~~ | UX | Fixed (`--brief`/`--summary`) |
| ~~`--limit` not aliased to `-n`~~ | UX | Fixed |
| ~~`peek --last N` lists sessions, not exchanges~~ | UX | Fixed (`--exchanges`) |
| ~~`peek` can't disambiguate main session from subagents~~ | Bug | Fixed |
| ~~`search` hard fails without embeddings (no FTS5 fallback)~~ | UX | Fixed |
| ~~Worktree sessions indistinguishable from main repo by workspace~~ | UX | Fixed (`[branch]` suffix, `--branch` filter) |
| Tool outputs in conversations hard to extract (agents pivot to git/files) | UX | Open |
| Can't search within live sessions (peek has no search, query needs ingest) | UX | Open |
| `siftd tag --last` requires count, should default to 1 | UX | Open |
| Live session tagging (`--session`) not discoverable from basic usage | UX | Open |
| No `./dev agent-close` to cleanup worktrees after merge | DX | Open |
| `peek` vs `query` confusion — agents try peek for ingested data | UX | Open |

**Investigation pattern:** Run agents in worktrees, use siftd to monitor their usage, document friction. Repeat.

## Recent Releases

| Version | Date | Highlights |
|---------|------|------------|
| v0.4.0 | pending | Live session tagging, binary filtering, workspace identity, status perf, CLI help groups, score explainability, doctor checks |
| v0.3.0 | 2026-01-30 | Relative dates, temporal weighting, numpy perf, incremental indexing |
| v0.2.0 | 2026-01-30 | Hard rules tests, privacy warnings, FTS5 error hints |
| v0.1.0 | 2026-01-28 | Initial release |

## Key Decisions

| Topic | Reference |
|-------|-----------|
| Architecture (adapters vs storage) | `principles:architecture` |
| Exchange-window chunking | `siftd search -w siftd "exchange window chunking"` |
| MMR diversity reranking | `siftd search -w siftd "MMR diversity"` |
| CLI as thin dispatcher | `principles:cli` — cli.py is 59 lines after full extraction |
| Content deduplication (hash-based blobs) | `siftd search -w siftd "content deduplication blob"` |
| Binary content filtering | Default on, config opt-out; metadata placeholder preserves type/size |
| Workspace identity | Git remote URL primary, fallback to resolved path for non-git dirs |
| Git worktree resolution | Worktrees resolve to main repo path; memoized with lru_cache |
| Adapter `can_handle()` | Location-aware, not just extension; prevents cross-adapter mismatches |
| Subtask session tracking | `~/.subtask/projects/{project}/internal/{task}/state.json` has `session_id`, `harness` |
| Config philosophy | "Defaults-with-escape-hatch": default on, config opt-out, not config-first (`01KGBXCAG8N8`) |

Full decision log: `siftd query -l decision:`

## Tracking

- **ROADMAP.md** — High-level phases and release themes
- **BACKLOG.md** — Minor issues and improvements (gitignored)
- **CHANGELOG.md** — Per-release details

## Dev Harness Structure

```
scripts/
├── lib/
│   ├── dev.sh         # Entry point (sources all libs, adds project helpers)
│   ├── log.sh         # log_info, log_error, etc.
│   ├── cli.sh         # cli_usage, cli_require_value, etc.
│   ├── paths.sh       # XDG paths, script path resolution
│   └── templates.sh   # Template {{placeholder}} injection
├── prompts/           # Agent prompt templates
│   ├── review.md      # Code review focus
│   ├── implement.md   # Implementation focus
│   ├── plan.md        # Planning focus
│   ├── research.md    # Exploration focus
│   └── interactive.md # Generic session, no specific task
├── agent.sh           # Launch agent with template
└── check.sh, lint.sh, test.sh, setup.sh, docs.sh
```

Add a command: create `scripts/<name>.sh` with `# DESC:` header.
Add a template: create `scripts/prompts/<name>.md` with `{{variable}}` placeholders.

Agent metadata tracked in `.agents/<branch>/`:
- `worktree` — path to worktree
- `session` — siftd session ID (discovered after launch)
- `started` — ISO timestamp

Note: Metadata preserved after worktree cleanup for historical reference.

---

*Pattern: docs as index, siftd as source of truth.*
