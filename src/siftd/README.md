# siftd

This file indexes the loose top-level modules that sit directly under
`src/siftd/` — the cross-cutting "core" that no subpackage owns. These are
leaf utilities and shared primitives with a single, well-scoped concern:
configuration (`config.py`, `config_sync.py`), search primitives (`search.py`),
pricing reference (`pricing.py`), ID generation (`ids.py`), XDG paths
(`paths.py`), unified error handling (`safecall.py`), and small shared helpers
(`dateparse.py`, `git.py`, `math.py`, `model_names.py`, `plugin_discovery.py`,
`credentials.py`, `backfill.py`, `skill_gen.py`). The generated table below maps
each to its first docstring line.

Navigation: every subpackage under `src/siftd/` (`adapters/`, `api/`, `cli/`,
`doctor/`, `output/`, `serve/`, `storage/`, …) has its own `README.md` with the
local conventions for that layer; the root `CLAUDE.md` is the entry ladder that
names them. Start there, not here, when you are looking for a *layer*; this file
is only for the modules that have no layer.

Adding code here vs. in a subpackage: a new top-level module earns its place
only when it is a genuinely cross-cutting primitive several layers depend on and
it belongs to none of them — the same test the existing entries pass. If the
code is the behavior of a layer (an adapter, a CLI command, a storage
operation, an output renderer), it goes in that subpackage instead. When a
loose module grows past a single concern into a cluster of related files,
promote it to a subpackage with its own `README.md` rather than letting the root
accumulate. After adding a module, run `./dev docs` to refresh the table below.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [backfill.py](backfill.py) | Backfill operations for siftd. |
| [config.py](config.py) | User configuration management for siftd. |
| [config_sync.py](config_sync.py) | Sync-specific configuration accessors. |
| [credentials.py](credentials.py) | Client-side OAuth token acquisition and storage for siftd. |
| [dateparse.py](dateparse.py) | Shared date parsing utilities for CLI filters and inline query fields. |
| [git.py](git.py) | Git repository utilities for workspace identity. |
| [ids.py](ids.py) | ULID generation (inline, no dependency). |
| [math.py](math.py) | Shared math utilities. |
| [model_names.py](model_names.py) | Model name parsing utilities. |
| [paths.py](paths.py) | XDG Base Directory paths for siftd. |
| [plugin_discovery.py](plugin_discovery.py) | Plugin discovery: shared utilities for loading drop-in and entry point plugins. |
| [pricing.py](pricing.py) | Pricing reference — the source of truth for model prices. |
| [safecall.py](safecall.py) | Safe operations that handle common failure modes. |
| [search.py](search.py) | Shared search primitives — candidate resolution, MMR reranking, temporal |
| [skill_gen.py](skill_gen.py) | Generate harness-appropriate instruction files from the bundled skill. |
<!-- gen:end -->
