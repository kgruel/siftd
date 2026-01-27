# SQL Queries

strata exposes the full SQLite database for custom queries. User-defined `.sql` files live in `~/.config/strata/queries/` and run via `strata query sql`.

## Using queries

```bash
# List available queries
strata query sql

# Run a query
strata query sql cost --var limit=20
```

Output is formatted as a text table by default, or `--json` for structured output.

## Writing queries

Place `.sql` files in `~/.config/strata/queries/`. Variables use `$name` syntax and are substituted via `string.Template`:

```sql
-- ~/.config/strata/queries/cost.sql
SELECT
    w.path AS workspace,
    m.name AS model,
    p.name AS provider,
    SUM(r.input_tokens) AS input_tokens,
    SUM(r.output_tokens) AS output_tokens,
    ROUND(SUM(
        r.input_tokens * pr.input_per_million / 1e6 +
        r.output_tokens * pr.output_per_million / 1e6
    ), 4) AS approx_cost_usd
FROM responses r
JOIN conversations c ON c.id = r.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
JOIN models m ON m.id = r.model_id
JOIN providers p ON p.id = r.provider_id
LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id
GROUP BY w.path, m.name, p.name
ORDER BY approx_cost_usd DESC
LIMIT $limit
```

Copy a built-in query as a starting point:

```bash
strata copy query cost
# Creates ~/.config/strata/queries/cost.sql
```

## Example queries

### Daily token usage

```sql
SELECT
    date(c.started_at) AS day,
    COUNT(DISTINCT c.id) AS conversations,
    SUM(r.input_tokens) AS input_tok,
    SUM(r.output_tokens) AS output_tok
FROM conversations c
JOIN responses r ON r.conversation_id = c.id
WHERE c.started_at >= date('now', '-30 days')
GROUP BY day
ORDER BY day DESC
```

### Tool usage by workspace

```sql
SELECT
    w.path AS workspace,
    t.name AS tool,
    COUNT(*) AS calls,
    SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) AS errors
FROM tool_calls tc
JOIN tools t ON t.id = tc.tool_id
JOIN conversations c ON c.id = tc.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
WHERE w.path LIKE '%$workspace%'
GROUP BY w.path, t.name
ORDER BY calls DESC
```

### Model comparison

```sql
SELECT
    m.name AS model,
    COUNT(DISTINCT c.id) AS conversations,
    AVG(r.input_tokens) AS avg_input,
    AVG(r.output_tokens) AS avg_output,
    AVG(r.output_tokens * 1.0 / NULLIF(r.input_tokens, 0)) AS output_ratio
FROM responses r
JOIN models m ON m.id = r.model_id
JOIN conversations c ON c.id = r.conversation_id
GROUP BY m.name
ORDER BY conversations DESC
```

### Long conversations

```sql
SELECT
    c.id,
    w.path AS workspace,
    COUNT(p.id) AS prompts,
    SUM(r.input_tokens + r.output_tokens) AS total_tokens
FROM conversations c
JOIN workspaces w ON w.id = c.workspace_id
JOIN prompts p ON p.conversation_id = c.id
JOIN responses r ON r.conversation_id = c.id
GROUP BY c.id
HAVING total_tokens > 100000
ORDER BY total_tokens DESC
LIMIT $limit
```

### Shell command analysis

```sql
-- What kinds of shell commands are being run?
SELECT
    tag.name AS category,
    COUNT(*) AS calls,
    COUNT(DISTINCT c.id) AS conversations
FROM tool_call_tags tct
JOIN tags tag ON tag.id = tct.tag_id
JOIN tool_calls tc ON tc.id = tct.tool_call_id
JOIN conversations c ON c.id = tc.conversation_id
WHERE tag.name LIKE 'shell:%'
GROUP BY tag.name
ORDER BY calls DESC
```

## Schema reference

See [data-model.md](data-model.md) for the full table schema. Key tables for writing queries:

- `conversations` — session-level data (workspace, timestamps)
- `responses` — token usage, model, provider
- `tool_calls` — tool invocations with input/result/status
- `workspaces`, `models`, `providers`, `tools` — vocabulary tables
- `tags`, `conversation_tags`, `tool_call_tags` — annotation layer
- `pricing` — cost approximation rates
