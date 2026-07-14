# siftd.peek

This package deliberately bypasses the database and the ingestion pipeline. It
reads live and recent session files straight from disk so you can inspect a
session that has not been (or may never be) ingested — the "what am I doing right
now" surface behind `siftd peek`. [`scanner.py`](scanner.py) discovers active
session files and extracts metadata, [`reader.py`](reader.py) parses a full
session, and [`follow.py`](follow.py) tails one live, rendering turns as they
arrive.

To read those files it reuses the adapter registry (`load_all_adapters`) — the
same parsers ingestion uses — so peek stays faithful to how each tool writes its
logs without duplicating parse logic. Because it never goes through
[`api/`](../api/) or [`storage/`](../storage/), nothing here is subject to the
DB-backed query path; the tradeoff is that peek sees only what is on disk, not
the enriched, deduplicated view the database holds.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [follow.py](follow.py) | Follow mode: tail a live session file and render turns as they arrive. |
| [reader.py](reader.py) | Session reader: parse full session detail from JSONL files. |
| [scanner.py](scanner.py) | Session scanner: discover and extract metadata from active session files. |
| [types.py](types.py) | Shared types for peek module. |
<!-- gen:end -->
