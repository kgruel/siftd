-- Daily activity summary
-- Usage: siftd query sql daily-activity --var limit=30

SELECT
    substr(c.started_at, 1, 10) as day,
    COUNT(*) as conversations,
    COALESCE(SUM((SELECT COUNT(*) FROM events e WHERE e.conversation_id = c.id AND e.kind = 'prompt')), 0) as prompts,
    COALESCE(SUM((SELECT COUNT(*) FROM events e WHERE e.conversation_id = c.id AND e.kind = 'response')), 0) as responses
FROM conversations c
GROUP BY day
ORDER BY day DESC
LIMIT $limit
