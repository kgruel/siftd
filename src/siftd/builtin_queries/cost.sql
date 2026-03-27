-- Approximate cost by workspace
-- Joins responses → models → pricing to compute token costs.
-- Results are APPROXIMATE: flat per-token pricing, cache read tokens treated as free.
--
-- Canonical cost formula: siftd.storage.sql_helpers.cost_expr_sql()
-- This file duplicates the formula in pure SQL (can't import Python).
-- If the formula changes, update both locations.
--
-- Usage: siftd query sql cost --var limit=50

SELECT
    w.path AS workspace,
    m.name AS model,
    pv.name AS provider,
    SUM(r.input_tokens) AS input_tokens,
    SUM(r.output_tokens) AS output_tokens,
    ROUND(SUM(
        (CASE
            WHEN COALESCE(r.input_tokens, 0) - COALESCE(ra_cache_read.cache_read, 0) < 0
                THEN 0
            ELSE COALESCE(r.input_tokens, 0) - COALESCE(ra_cache_read.cache_read, 0)
        END) * COALESCE(pr.input_per_mtok, 0)
        + COALESCE(r.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
    ) / 1000000.0, 4) AS approx_cost_usd
FROM responses r
JOIN conversations c ON c.id = r.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
LEFT JOIN models m ON m.id = r.model_id
LEFT JOIN providers pv ON pv.id = r.provider_id
LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id
LEFT JOIN (
    SELECT response_id, MAX(CAST(value AS INTEGER)) AS cache_read
    FROM response_attributes
    WHERE key = 'cache_read_input_tokens'
    GROUP BY response_id
) ra_cache_read ON ra_cache_read.response_id = r.id
WHERE r.input_tokens IS NOT NULL OR r.output_tokens IS NOT NULL
GROUP BY w.path, m.name, pv.name
ORDER BY approx_cost_usd DESC
LIMIT $limit
