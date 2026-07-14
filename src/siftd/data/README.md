# siftd.data

This directory holds version-controlled reference data — facts that are identical on every machine and sourced externally, such as per-model prices and the OpenAI-compatible embedding-endpoint presets. The defining property is that a file here is *reference data, not state*: the corresponding SQLite tables (for example `pricing`) are projections rebuilt from these files on every database open via UPSERT. They are never frozen and never synced between machines, so correcting a value means editing the TOML here — or the per-user override at `~/.config/siftd/` — and reopening the database, which reprices or re-reads on the next run.

Keep the boundary clean: only cross-machine facts belong here. Anything a user chooses per machine (which embedding backend, which API key, a dimension override, a price you override locally) lives in `~/.config/siftd/config.toml` or the user override files, not in this checked-in data. Each file's header comment documents its own schema and override path; read it before editing, and treat these as the source of truth the runtime derives from rather than a cache you can regenerate arbitrarily.

<!-- gen:begin files -->
<sub>generated from the `src/siftd/data` directory — run <code>./dev docs</code></sub>

| File | Description |
|------|-------------|
| [embed_presets.toml](embed_presets.toml) | siftd embedding presets — reference data for the generic remote embedding client. |
| [pricing.toml](pricing.toml) | siftd pricing reference — the version-controlled source of truth for model prices. |
<!-- gen:end -->
