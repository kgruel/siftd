-- Usage breakdown by harness (Claude Code, Gemini CLI, etc.)
-- Usage: siftd query sql harness-stats

-- One events scan per conversation (uses idx_events_conversation_kind) with
-- per-kind conditional aggregation. The previous form joined events three
-- times keyed only on conversation_id, fanning out to a P*R*T cross-product
-- per conversation before GROUP BY collapsed it. That produced billions of
-- intermediate rows on a real DB and was unusable, though the counts matched.
SELECT
    h.name as harness,
    COUNT(DISTINCT c.id) as conversations,
    COUNT(DISTINCT CASE WHEN e.kind = 'prompt' THEN e.id END) as prompts,
    COUNT(DISTINCT CASE WHEN e.kind = 'response' THEN e.id END) as responses,
    COUNT(DISTINCT CASE WHEN e.kind = 'tool_call' THEN e.id END) as tool_calls
FROM harnesses h
LEFT JOIN conversations c ON c.harness_id = h.id
LEFT JOIN events e ON e.conversation_id = c.id
GROUP BY h.id
ORDER BY conversations DESC
