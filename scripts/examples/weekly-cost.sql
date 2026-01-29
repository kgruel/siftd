-- Weekly cost report by workspace
-- Groups conversations by ISO week, sums tokens, computes approximate cost.
--
-- Usage: siftd query sql weekly-cost weeks=4
--        siftd query sql weekly-cost weeks=12 workspace=myproject

SELECT
    strftime('%Y-W%W', c.started_at) AS week,
    CASE
        WHEN w.path = '' THEN '(root)'
        ELSE SUBSTR(w.path, -30)  -- last 30 chars of path
    END AS workspace,
    COUNT(DISTINCT c.id) AS conversations,
    SUM(r.input_tokens) AS input_tokens,
    SUM(r.output_tokens) AS output_tokens,
    ROUND(SUM(
        COALESCE(r.input_tokens, 0) * COALESCE(pr.input_per_mtok, 0)
        + COALESCE(r.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
    ) / 1000000.0, 2) AS approx_cost_usd
FROM conversations c
JOIN workspaces w ON w.id = c.workspace_id
JOIN responses r ON r.conversation_id = c.id
LEFT JOIN pricing pr ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id
WHERE c.started_at >= date('now', '-' || $weeks || ' weeks')
  AND ($workspace IS NULL OR w.path LIKE '%' || $workspace || '%')
GROUP BY week, w.path
ORDER BY week DESC, approx_cost_usd DESC
