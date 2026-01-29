Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Install

```bash
uv pip install .           # core (FTS5 search, tags, queries)
uv pip install .[embed]    # with semantic search (siftd ask)
```

## Architecture

Core loop: **Ingest → Store → Query**

- **Adapters** own parsing and raw format knowledge. Storage is adapter-agnostic.
- **Storage** is normalized SQLite. Schema is fixed for core entities, extensible via `*_attributes` tables.
- **Queries** are user-defined `.sql` files with `$var` substitution. The system is a data platform, not a reporting tool.

## Design Principles

1. **Manual first, automate when patterns emerge** — labels are user-applied, enrichment is deferred, cost is approximate. Don't build automation until real usage reveals what's worth automating.
2. **Query-time computation over stored redundancy** — cost is derived via JOIN, not pre-computed. Avoids stale data and schema coupling.
3. **Attributes for variable metadata** — when the field set varies by provider or adapter, use key/value `*_attributes` tables instead of adding nullable columns.
4. **Adapters are the parsing boundary** — each adapter knows its raw format, dedup strategy, and provider source. Everything downstream is normalized.
5. **Approximate is fine when labeled** — approximate cost is useful. Don't over-engineer precision until billing context demands it.

## Branching

- Work in a `wip/<topic>` branch during sessions. Handoff updates, test scaffolding, and iterative commits go here.
- Subtask merges target `main` independently (they're isolated worktrees).
- Merge/rebase WIP to main when the work is ready to ship.

## Conventions

- `commit=False` default on storage functions; caller controls transaction boundaries
- ULIDs for all primary keys
- XDG paths: data `~/.local/share/siftd`, config `~/.config/siftd`
- New CLI commands follow existing argparse patterns in `src/cli.py`
- New adapters implement `can_handle(source)`, `parse(source)`, `discover()`, set `HARNESS_SOURCE`
- Queries go in `~/.config/siftd/queries/*.sql`, use `$var` for parameters

## Agent Memory (siftd)

Search past conversations:
```
siftd ask "your query"              # semantic search
siftd ask -w projectname "query"    # filter by workspace
siftd query <id>                    # drill down into conversation
```

Tag useful findings:
```
siftd tag <id> research:<topic>   # bookmark for later
siftd query -l research:<topic>   # retrieve tagged
```

Tag conventions:
- `research:*` — Investigation findings worth preserving
- `useful:*` — General bookmarks (useful:pattern, useful:example)
- `decision:*` — Key architectural/design decisions
- `handoff:update` — Sessions that modified HANDOFF.md
- `rationale:*` — Why we chose X over Y
- `genesis:*` — First mention of a concept

When you find something useful via `siftd ask`, tag it before moving on.

Before ending a session that updates HANDOFF.md:
```
siftd tag --last handoff:update
```
