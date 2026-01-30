-- Quick overview of your siftd data
-- Usage: siftd query sql overview

SELECT
    (SELECT COUNT(*) FROM conversations) as conversations,
    (SELECT COUNT(*) FROM prompts) as prompts,
    (SELECT COUNT(*) FROM responses) as responses,
    (SELECT COUNT(*) FROM tool_calls) as tool_calls,
    (SELECT COUNT(DISTINCT workspace_id) FROM conversations WHERE workspace_id IS NOT NULL) as workspaces,
    (SELECT COUNT(DISTINCT model_id) FROM responses WHERE model_id IS NOT NULL) as models_used
