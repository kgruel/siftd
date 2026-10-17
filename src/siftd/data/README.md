# siftd.data

This directory holds version-controlled reference data — facts that are identical on every machine and sourced externally, such as per-model prices and the OpenAI-compatible embedding-endpoint presets. The defining property is that a file here is *reference data, not state*: the corresponding SQLite tables (for example `pricing`) are projections rebuilt from these files on every database open via UPSERT. They are never frozen and never synced between machines. `pricing.toml` is owned by `./dev pricing-sync`, which refreshes the entries under anthropic, openai and google from models.dev. It runs as a release step, not in CI, so a tag publishes exactly what a person reviewed. Two things correct a price: a `pinned = true` entry, which the sync never overwrites and reports when upstream disagrees with, or a value in the per-user override at `~/.config/siftd/`. Either way, reopening the database reprices or re-reads on the next run. A price is filed per `(model, provider)`; a response is priced at the provider that served it when one is filed, else at the model's maker (`models.creator`), so a maker's entry also prices the same model reached through a router or multiplexer (see the storage README).

Keep the boundary clean: only cross-machine facts belong here. Anything a user chooses per machine (which embedding backend, which API key, a dimension override, a price you override locally) lives in `~/.config/siftd/config.toml` or the user override files, not in this checked-in data. Each file's header comment documents its own schema and override path; read it before editing, and treat these as the source of truth the runtime derives from rather than a cache you can regenerate arbitrarily.

<!-- gen:begin files -->
<sub>generated from the `src/siftd/data` directory — run <code>./dev docs</code></sub>

| File | Description |
|------|-------------|
| [embed_presets.toml](embed_presets.toml) | siftd embedding presets — reference data for the generic remote embedding client. |
| [pricing.toml](pricing.toml) | siftd pricing reference — the version-controlled source of truth for model prices. |
<!-- gen:end -->
