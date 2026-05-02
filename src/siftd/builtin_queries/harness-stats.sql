-- Usage breakdown by harness (Claude Code, Gemini CLI, etc.)
-- Usage: siftd query sql harness-stats

SELECT
    h.name as harness,
    COUNT(DISTINCT c.id) as conversations,
    COUNT(DISTINCT ep.id) as prompts,
    COUNT(DISTINCT er.id) as responses,
    COUNT(DISTINCT et.id) as tool_calls
FROM harnesses h
LEFT JOIN conversations c ON c.harness_id = h.id
LEFT JOIN events ep ON ep.conversation_id = c.id AND ep.kind = 'prompt'
LEFT JOIN events er ON er.conversation_id = c.id AND er.kind = 'response'
LEFT JOIN events et ON et.conversation_id = c.id AND et.kind = 'tool_call'
GROUP BY h.id
ORDER BY conversations DESC
