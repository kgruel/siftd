# siftd.builtin_queries

These are the report templates siftd ships with. They are user-facing starting points, not internal machinery: a user runs one with `siftd report <name>` or copies it into `~/.config/siftd/queries/` via `siftd copy query <name>` to customize. Keep them distinct from storage's internal read queries (`storage/queries.py`) — these are meant to be read, run, and forked by people, so favor clarity and portable SQL over cleverness.

The conventions here are load-bearing. The first `--` comment line is the human description surfaced in the `siftd report` listing (that is what the generated table below reflects). Parameterize with `$var` or `:var` substitution, the same syntax user queries use. And because reports execute as pure SQL against the database, they cannot call into Python — so anything derived (the cost formula, for instance) must be reproduced inline in SQL; where a query duplicates canonical logic from the codebase, its header notes the source so the two stay reconcilable.

See [Storage — Direct SQL access](../../../docs/concepts/storage.md#direct-sql-access) for the reports surface and the schema these queries run against.

<!-- gen:begin files -->
<sub>generated from the `src/siftd/builtin_queries` directory — run <code>./dev docs</code></sub>

| File | Description |
|------|-------------|
| [__init__.py](__init__.py) | Built-in SQL query templates shipped with siftd. |
| [cost.sql](cost.sql) | Approximate cost by workspace |
| [daily-activity.sql](daily-activity.sql) | Daily activity summary |
| [harness-stats.sql](harness-stats.sql) | Usage breakdown by harness (Claude Code, Gemini CLI, etc.) |
| [model-usage.sql](model-usage.sql) | Model usage with token breakdown |
| [overview.sql](overview.sql) | Quick overview of your siftd data |
| [session-tools.sql](session-tools.sql) | Tool calls for a session with character counts |
| [shell-analysis.sql](shell-analysis.sql) | Shell command granularity analysis |
| [tool-usage.sql](tool-usage.sql) | Tool usage frequency and error rates |
<!-- gen:end -->
