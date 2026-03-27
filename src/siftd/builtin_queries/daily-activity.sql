-- Daily activity summary
-- Usage: siftd query sql daily-activity --var limit=30

SELECT
    substr(c.started_at, 1, 10) as day,
    COUNT(*) as conversations,
    COALESCE(SUM((SELECT COUNT(*) FROM prompts p WHERE p.conversation_id = c.id)), 0) as prompts,
    COALESCE(SUM((SELECT COUNT(*) FROM responses r WHERE r.conversation_id = c.id)), 0) as responses
FROM conversations c
GROUP BY day
ORDER BY day DESC
LIMIT $limit
