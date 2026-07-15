# siftd.domain

These are siftd's pure data objects: the nested `Conversation → Prompt → Response → ToolCall` model in [models.py](models.py), plus the shared types other layers exchange (peek, progress, search, source, sync). They are plain dataclasses, decoupled from persistence — adapters produce them, storage consumes them, and renderers read them, but the objects themselves know nothing about any of those layers.

The boundary to hold when editing here: no storage, database, network, or CLI imports. Domain stays dependency-free so it can be the common vocabulary every layer depends on without a cycle. A couple of details are load-bearing rather than obvious — `ToolCall.attributes` and `Response.attributes` are the local form of rows that persist into storage's polymorphic `attributes` table (used, for example, to correlate background/async tool calls), and `Usage` token fields are `int | None` where `None` means "not reported," distinct from a real zero. Adding a field here ripples outward to adapters, storage serialization, and renderers, so prefer extending an existing model over introducing a parallel one.

See [Data Model](../../../docs/concepts/data-model.md) for how this hierarchy maps to what tools actually record.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [models.py](models.py) | Domain models for siftd. |
| [peek.py](peek.py) | Shared peek types used by adapters and the peek module. |
| [progress.py](progress.py) | The progress-event contract — one typed stream every action command emits. |
| [search_types.py](search_types.py) | Shared search-domain types used across API/search/storage/output layers. |
| [shell_categories.py](shell_categories.py) | Shell command categorization logic. |
| [source.py](source.py) | Source abstraction for adapter inputs. |
| [sync.py](sync.py) | Domain models for sync operations. |
<!-- gen:end -->
