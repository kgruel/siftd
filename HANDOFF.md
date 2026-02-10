# siftd — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and semantic search.

## Current Focus

**v0.4.4 published and patched.** All CI green, Homebrew tap updated.

Next:
- [ ] **`db merge`** — multi-device merge (design understood, `db slice` now validated and working)
- [ ] **Break down `cmd_search()`** — 367 lines, works but long
- [ ] **`siftd tags` list view temporal filtering** — needs new storage query (conversation time vs applied-at semantics TBD)
- [ ] **NULL workspace_path asymmetry** in `find_active_session` — document as intentional

Previous sessions:
- [x] Post-0.4.4: `db slice` FK fix (ALTER TABLE column order), tags filter pipeline, Homebrew formula generation rewrite
- [x] `siftd db` namespace, shared filter pipeline, slice export, deprecation wrappers
- [x] CLI quality cleanup: connection leaks, API wrappers, architecture violations, module extraction
- [x] CLI decomposition: cli_meta, cli_tags, cli_query, cli_data, cli_sessions, cli_common
- [x] Worktree identity, agent monitoring patterns, CI fixes

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
| ~~`siftd tag --last` requires count, should default to 1~~ | UX | Fixed (0.4.3) |
| Live session tagging (`--session`) not discoverable from basic usage | UX | Open |
| No `./dev agent-close` to cleanup worktrees after merge | DX | Open |
| `peek` vs `query` confusion — agents try peek for ingested data | UX | Open |

**Investigation pattern:** Run agents in worktrees, use siftd to monitor their usage, document friction. Repeat.

## Recent Releases

| Version | Date | Highlights |
|---------|------|------------|
| v0.4.4 | 2026-02-10 | `siftd db` namespace, shared filter pipeline, slice export, Codex tokens, cache-aware cost |
| v0.4.3 | 2026-02-09 | Narrative detail view, Turn model, CLI decomposition, architecture enforcement |
| v0.4.0 | 2026-02-05 | Live session tagging, binary filtering, workspace identity, status perf, CLI help groups, score explainability, doctor checks |
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
