# siftd

<!-- TODO(preamble): authored in slice 3 -->
Core modules (config, search, pricing, ids, paths, safecall, …).

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
