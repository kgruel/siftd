-- Tool usage frequency and error rates
-- Usage: siftd query sql tool-usage --var limit=20

SELECT
    t.name as tool,
    COUNT(*) as uses,
    SUM(CASE WHEN etc.status = 'error' THEN 1 ELSE 0 END) as errors,
    ROUND(100.0 * SUM(CASE WHEN etc.status = 'error' THEN 1 ELSE 0 END) / COUNT(*), 1) as error_pct
FROM events e
JOIN event_tool_call etc ON etc.event_id = e.id
JOIN tools t ON etc.tool_id = t.id
WHERE e.kind = 'tool_call'
GROUP BY t.id
ORDER BY uses DESC
LIMIT $limit
