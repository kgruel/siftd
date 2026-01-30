-- Usage breakdown by harness (Claude Code, Gemini CLI, etc.)
-- Usage: siftd query sql harness-stats

SELECT
    h.name as harness,
    COUNT(DISTINCT c.id) as conversations,
    COUNT(DISTINCT p.id) as prompts,
    COUNT(DISTINCT r.id) as responses,
    COUNT(DISTINCT tc.id) as tool_calls
FROM harnesses h
LEFT JOIN conversations c ON c.harness_id = h.id
LEFT JOIN prompts p ON p.conversation_id = c.id
LEFT JOIN responses r ON r.conversation_id = c.id
LEFT JOIN tool_calls tc ON tc.conversation_id = c.id
GROUP BY h.id
ORDER BY conversations DESC
