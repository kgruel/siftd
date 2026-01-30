-- Model usage with token breakdown
-- Usage: siftd query sql model-usage --var limit=20

SELECT
    m.raw_name as model,
    COUNT(*) as responses,
    ROUND(SUM(r.input_tokens) / 1000000.0, 2) as input_mtok,
    ROUND(SUM(r.output_tokens) / 1000000.0, 2) as output_mtok,
    ROUND(SUM(r.input_tokens + COALESCE(r.output_tokens, 0)) / 1000000.0, 2) as total_mtok
FROM responses r
JOIN models m ON r.model_id = m.id
GROUP BY m.raw_name
ORDER BY total_mtok DESC
LIMIT $limit
