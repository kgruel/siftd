-- Tool calls for a session with character counts
-- Shows each tool invocation in chronological order with input/output sizes.
-- Character counts are a rough proxy for token usage per tool call.
--
-- Usage: siftd query sql session-tools --var session=<conversation_id_or_external_id>

SELECT
    e.timestamp,
    t.name AS tool,
    etc.status,
    LENGTH(etc.input) AS input_chars,
    COALESCE(LENGTH(cb.content), 0) AS result_chars,
    LENGTH(etc.input) + COALESCE(LENGTH(cb.content), 0) AS total_chars
FROM events e
JOIN event_tool_call etc ON etc.event_id = e.id
JOIN conversations c ON c.id = e.conversation_id
LEFT JOIN tools t ON t.id = etc.tool_id
LEFT JOIN content_blobs cb ON cb.hash = etc.result_hash
WHERE e.kind = 'tool_call'
  AND (c.id = :session OR c.external_id = :session)
ORDER BY e.timestamp
