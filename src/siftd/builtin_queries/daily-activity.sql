-- Daily activity summary
-- Usage: siftd query sql daily-activity --var limit=30

SELECT
    substr(c.started_at, 1, 10) as day,
    COUNT(DISTINCT c.id) as conversations,
    COUNT(p.id) as prompts,
    COUNT(r.id) as responses
FROM conversations c
LEFT JOIN prompts p ON p.conversation_id = c.id
LEFT JOIN responses r ON r.conversation_id = c.id
GROUP BY day
ORDER BY day DESC
LIMIT $limit
