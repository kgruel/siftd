-- Approximate cost by workspace
-- Reads the usage rollup, where each (conversation, model, provider) group already
-- carries its cost: the one place siftd decides which price a response gets
-- (siftd.storage.sql_helpers.price_provider_id_sql: the serving provider's price,
-- else the model maker's) and how its cache tokens are billed. This file does not
-- copy that logic, so it cannot drift from it.
--
-- input_tokens is the TRUE total (uncached + cache_read + cache_creation).
-- approx_cost_usd is NULL for a group no price covers: unpriced is not free.
-- The rollup is rebuilt by `siftd ingest` and `siftd backfill --pricing`.
--
-- Usage: siftd query sql cost --var limit=50

SELECT
    w.path AS workspace,
    m.name AS model,
    pv.name AS provider,
    SUM(u.input_tokens) AS input_tokens,
    SUM(u.output_tokens) AS output_tokens,
    ROUND(SUM(u.cost), 4) AS approx_cost_usd
FROM usage_by_conv_model u
JOIN conversations c ON c.id = u.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
LEFT JOIN models m ON m.id = u.model_id
LEFT JOIN providers pv ON pv.id = u.provider_id
GROUP BY w.path, m.name, pv.name
HAVING SUM(u.responses_with_tokens) > 0
ORDER BY approx_cost_usd DESC
LIMIT $limit
