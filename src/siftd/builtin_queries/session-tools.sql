-- Tool calls for a session with character counts
-- Shows each tool invocation in chronological order with input/output sizes.
-- Character counts are a rough proxy for token usage per tool call.
--
-- Usage: siftd query sql session-tools --var session=<conversation_id_or_external_id>

SELECT
    tc.timestamp,
    t.name AS tool,
    tc.status,
    LENGTH(tc.input) AS input_chars,
    COALESCE(LENGTH(cb.content), LENGTH(tc.result), 0) AS result_chars,
    LENGTH(tc.input) + COALESCE(LENGTH(cb.content), LENGTH(tc.result), 0) AS total_chars
FROM tool_calls tc
JOIN conversations c ON c.id = tc.conversation_id
LEFT JOIN tools t ON t.id = tc.tool_id
LEFT JOIN content_blobs cb ON cb.hash = tc.result_hash
WHERE c.id = :session OR c.external_id = :session
ORDER BY tc.timestamp
