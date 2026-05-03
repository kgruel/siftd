-- Model usage with token breakdown
-- Usage: siftd query sql model-usage --var limit=20

SELECT
    m.raw_name as model,
    COUNT(*) as responses,
    ROUND(SUM(er.input_tokens) / 1000000.0, 2) as input_mtok,
    ROUND(SUM(er.output_tokens) / 1000000.0, 2) as output_mtok,
    ROUND(SUM(COALESCE(er.input_tokens, 0) + COALESCE(er.output_tokens, 0)) / 1000000.0, 2) as total_mtok
FROM events e
JOIN event_response er ON er.event_id = e.id
JOIN models m ON er.model_id = m.id
WHERE e.kind = 'response'
GROUP BY m.raw_name
ORDER BY total_mtok DESC
LIMIT $limit
