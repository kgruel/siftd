-- Approximate cost by workspace
-- Joins responses → models → pricing to compute token costs.
-- Results are APPROXIMATE: assumes flat per-token pricing, no cache discount.
--
-- Usage: siftd query sql cost --var limit=50

SELECT
    w.path AS workspace,
    m.name AS model,
    pv.name AS provider,
    SUM(r.input_tokens) AS input_tokens,
    SUM(r.output_tokens) AS output_tokens,
    ROUND(SUM(
        COALESCE(r.input_tokens, 0) * COALESCE(pr.input_per_mtok, 0)
        + COALESCE(r.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
    ) / 1000000.0, 4) AS approx_cost_usd
FROM responses r
JOIN conversations c ON c.id = r.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
LEFT JOIN models m ON m.id = r.model_id
LEFT JOIN providers pv ON pv.id = r.provider_id
LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id
WHERE r.input_tokens IS NOT NULL
GROUP BY w.path, m.name, pv.name
ORDER BY approx_cost_usd DESC
LIMIT $limit

