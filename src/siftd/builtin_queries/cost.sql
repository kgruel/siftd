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
    SUM(er.input_tokens) AS input_tokens,
    SUM(er.output_tokens) AS output_tokens,
    ROUND(SUM(
        (CASE
            WHEN COALESCE(er.input_tokens, 0) - COALESCE(ra_cache_read.cache_read, 0) < 0
                THEN 0
            ELSE COALESCE(er.input_tokens, 0) - COALESCE(ra_cache_read.cache_read, 0)
        END) * COALESCE(pr.input_per_mtok, 0)
        + COALESCE(er.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
    ) / 1000000.0, 4) AS approx_cost_usd
FROM events e
JOIN event_response er ON er.event_id = e.id
JOIN conversations c ON c.id = e.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
LEFT JOIN models m ON m.id = er.model_id
LEFT JOIN providers pv ON pv.id = er.provider_id
LEFT JOIN pricing pr ON pr.model_id = er.model_id AND pr.provider_id = er.provider_id
LEFT JOIN (
    SELECT response_id, MAX(CAST(value AS INTEGER)) AS cache_read
    FROM response_attributes
    WHERE key = 'cache_read_input_tokens'
    GROUP BY response_id
) ra_cache_read ON ra_cache_read.response_id = e.id
WHERE e.kind = 'response'
  AND (er.input_tokens IS NOT NULL OR er.output_tokens IS NOT NULL)
GROUP BY w.path, m.name, pv.name
ORDER BY approx_cost_usd DESC
LIMIT $limit
