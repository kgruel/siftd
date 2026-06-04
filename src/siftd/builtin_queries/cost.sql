-- Approximate cost by workspace
-- Joins responses → models → pricing to compute token costs (cache-aware).
--
-- Canonical cost formula: siftd.storage.sql_helpers.cost_expr_sql() +
-- uncached_input_sql(). This file duplicates that formula in pure SQL (can't
-- import Python). If either changes, update both — test_cost_expr_sql_parity guards it.
--
-- input_tokens here is the TRUE total (uncached + cache_read + cache_creation),
-- normalized across the provider conventions. Anthropic input excludes cache and is
-- additive, OpenAI input includes cache_read as a subset. Cost bills four disjoint
-- components. Cache rates default to the standard multiple of the input rate
-- (read 0.1x, creation 1.25x) when the override pricing column is NULL.
--
-- Usage: siftd query sql cost --var limit=50

SELECT
    w.path AS workspace,
    m.name AS model,
    pv.name AS provider,
    -- True total input: uncached (per convention) + both cache components.
    SUM(
        (CASE
            WHEN h.source = 'anthropic' THEN COALESCE(er.input_tokens, 0)
            WHEN COALESCE(er.input_tokens, 0) < COALESCE(rc.cache_read, 0)
                THEN COALESCE(er.input_tokens, 0)
            ELSE COALESCE(er.input_tokens, 0) - COALESCE(rc.cache_read, 0)
        END)
        + COALESCE(rc.cache_read, 0) + COALESCE(rk.cache_creation, 0)
    ) AS input_tokens,
    SUM(er.output_tokens) AS output_tokens,
    ROUND(SUM(
        -- uncached input @ input rate
        (CASE
            WHEN h.source = 'anthropic' THEN COALESCE(er.input_tokens, 0)
            WHEN COALESCE(er.input_tokens, 0) < COALESCE(rc.cache_read, 0)
                THEN COALESCE(er.input_tokens, 0)
            ELSE COALESCE(er.input_tokens, 0) - COALESCE(rc.cache_read, 0)
        END) * COALESCE(pr.input_per_mtok, 0)
        -- cache creation @ 1.25× input (or override)
        + COALESCE(rk.cache_creation, 0)
            * COALESCE(pr.cache_creation_per_mtok, COALESCE(pr.input_per_mtok, 0) * 1.25)
        -- cache read @ 0.1× input (or override)
        + COALESCE(rc.cache_read, 0)
            * COALESCE(pr.cache_read_per_mtok, COALESCE(pr.input_per_mtok, 0) * 0.1)
        -- output @ output rate
        + COALESCE(er.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
    ) / 1000000.0, 4) AS approx_cost_usd
FROM events e
JOIN event_response er ON er.event_id = e.id
JOIN conversations c ON c.id = e.conversation_id
JOIN workspaces w ON w.id = c.workspace_id
LEFT JOIN harnesses h ON h.id = c.harness_id
LEFT JOIN models m ON m.id = er.model_id
LEFT JOIN providers pv ON pv.id = er.provider_id
LEFT JOIN pricing pr ON pr.model_id = er.model_id AND pr.provider_id = er.provider_id
LEFT JOIN (
    SELECT target_id, MAX(CAST(value AS INTEGER)) AS cache_read
    FROM attributes
    WHERE target_kind = 'response' AND key = 'cache_read_input_tokens'
    GROUP BY target_id
) rc ON rc.target_id = e.id
LEFT JOIN (
    SELECT target_id, MAX(CAST(value AS INTEGER)) AS cache_creation
    FROM attributes
    WHERE target_kind = 'response' AND key = 'cache_creation_input_tokens'
    GROUP BY target_id
) rk ON rk.target_id = e.id
WHERE e.kind = 'response'
  AND (er.input_tokens IS NOT NULL OR er.output_tokens IS NOT NULL)
GROUP BY w.path, m.name, pv.name
ORDER BY approx_cost_usd DESC
LIMIT $limit
