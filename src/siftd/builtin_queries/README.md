# siftd.builtin_queries

<!-- TODO(preamble): authored in slice 3 -->
Built-in SQL query templates shipped with siftd (siftd copy query <name>).

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
