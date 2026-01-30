-- Tool usage frequency and error rates
-- Usage: siftd query sql tool-usage --var limit=20

SELECT
    t.name as tool,
    COUNT(*) as uses,
    SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) as errors,
    ROUND(100.0 * SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) / COUNT(*), 1) as error_pct
FROM tool_calls tc
JOIN tools t ON tc.tool_id = t.id
GROUP BY t.id
ORDER BY uses DESC
LIMIT $limit
