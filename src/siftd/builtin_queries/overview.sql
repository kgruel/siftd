-- Quick overview of your siftd data
-- Usage: siftd query sql overview

SELECT
    (SELECT COUNT(*) FROM conversations) as conversations,
    (SELECT COUNT(*) FROM events WHERE kind = 'prompt') as prompts,
    (SELECT COUNT(*) FROM events WHERE kind = 'response') as responses,
    (SELECT COUNT(*) FROM events WHERE kind = 'tool_call') as tool_calls,
    (SELECT COUNT(DISTINCT workspace_id) FROM conversations WHERE workspace_id IS NOT NULL) as workspaces,
    (SELECT COUNT(DISTINCT er.model_id) FROM event_response er WHERE er.model_id IS NOT NULL) as models_used
