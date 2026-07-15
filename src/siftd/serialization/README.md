# siftd.serialization

This package exists to hold a deliberate architecture boundary. It serializes
normalized siftd data (conversations, turns, narrative blocks, stats, health)
into JSON-safe dicts, and it sits alongside `domain/` and `utilities/` so any
higher layer may import it. Its reason to be separate from
[`output/`](../output/) is that the serve layer must not import `output/` — doing
so would drag painted and the terminal renderer into the HTTP server — so serve's
JSON path lives here instead. [`serve_fmt.py`](serve_fmt.py) is the serve-side
counterpart to `output/json_fmt.py`.

The load-bearing constraint is wire-format parity: the JSON a serve route emits
and the JSON `siftd <cmd> --json` emits must be the same shape, because they
serialize the same domain objects through this layer. When you change a
serializer here, change (or verify) its `output/json_fmt` peer in the same edit,
and lean on the serializer-drift tests
([`tests/test_search_serializer_drift.py`](../../../tests/test_search_serializer_drift.py))
that guard the two from diverging.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [backfill.py](backfill.py) | Backfill result serialization helpers. |
| [conversations.py](conversations.py) | Canonical JSON serialization for conversation objects. |
| [events.py](events.py) | JSON serialization for event detail. |
| [health.py](health.py) | Canonical JSON serialization for serve health status. |
| [ingest.py](ingest.py) | Ingest result serialization helpers. |
| [narrative.py](narrative.py) | Narrative walker and JSON emitter — shared decision logic for serializing narratives. |
| [serve_fmt.py](serve_fmt.py) | Serve format — JSON serialization for serve route dispatch. |
| [stats.py](stats.py) | Canonical JSON serialization for database statistics. |
| [tags.py](tags.py) | Tag mutation serialization helpers for serve routes. |
<!-- gen:end -->
