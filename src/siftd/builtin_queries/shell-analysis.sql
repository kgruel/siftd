-- Shell command granularity analysis
-- Breaks down tagged shell commands by tool and action.
-- Used to evaluate whether hierarchical tags would add value.
--
-- Usage: siftd query sql shell-analysis

-- 1. Tag distribution
SELECT
    t.name AS tag,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM tool_call_tags tct
JOIN tags t ON t.id = tct.tag_id
WHERE t.name LIKE 'shell:%'
GROUP BY t.name
ORDER BY count DESC;

-- 2. Tool breakdown within shell:vcs (first word after cd stripping)
SELECT
    CASE
        WHEN etc.input LIKE '%git %' OR etc.input LIKE '%git\n%' THEN 'git'
        WHEN etc.input LIKE '%yadm %' THEN 'yadm'
        WHEN etc.input LIKE '%gh %' THEN 'gh'
        ELSE 'other'
    END AS tool,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM events e
JOIN event_tool_call etc ON etc.event_id = e.id
JOIN tool_call_tags tct ON tct.tool_call_id = e.id
JOIN tags t ON t.id = tct.tag_id
WHERE e.kind = 'tool_call' AND t.name = 'shell:vcs'
GROUP BY tool
ORDER BY count DESC;

-- 3. Git action distribution (subcommand after 'git')
SELECT
    CASE
        WHEN etc.input LIKE '%git add%' THEN 'add'
        WHEN etc.input LIKE '%git log%' THEN 'log'
        WHEN etc.input LIKE '%git status%' THEN 'status'
        WHEN etc.input LIKE '%git diff%' THEN 'diff'
        WHEN etc.input LIKE '%git commit%' THEN 'commit'
        WHEN etc.input LIKE '%git show%' THEN 'show'
        WHEN etc.input LIKE '%git checkout%' THEN 'checkout'
        WHEN etc.input LIKE '%git push%' THEN 'push'
        WHEN etc.input LIKE '%git branch%' THEN 'branch'
        WHEN etc.input LIKE '%git pull%' THEN 'pull'
        WHEN etc.input LIKE '%git merge%' THEN 'merge'
        WHEN etc.input LIKE '%git worktree%' THEN 'worktree'
        WHEN etc.input LIKE '%git stash%' THEN 'stash'
        ELSE 'other'
    END AS action,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM events e
JOIN event_tool_call etc ON etc.event_id = e.id
JOIN tool_call_tags tct ON tct.tool_call_id = e.id
JOIN tags t ON t.id = tct.tag_id
WHERE e.kind = 'tool_call' AND t.name = 'shell:vcs'
  AND (etc.input LIKE '%git %' OR etc.input LIKE '%git\n%')
GROUP BY action
ORDER BY count DESC;

-- 4. Git read vs write classification
SELECT
    CASE
        WHEN etc.input LIKE '%git status%'
          OR etc.input LIKE '%git log%'
          OR etc.input LIKE '%git diff%'
          OR etc.input LIKE '%git show%'
          OR etc.input LIKE '%git branch%' THEN 'read'
        WHEN etc.input LIKE '%git add%'
          OR etc.input LIKE '%git commit%'
          OR etc.input LIKE '%git push%'
          OR etc.input LIKE '%git pull%'
          OR etc.input LIKE '%git merge%'
          OR etc.input LIKE '%git rebase%'
          OR etc.input LIKE '%git reset%' THEN 'write'
        ELSE 'other'
    END AS mode,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM events e
JOIN event_tool_call etc ON etc.event_id = e.id
JOIN tool_call_tags tct ON tct.tool_call_id = e.id
JOIN tags t ON t.id = tct.tag_id
WHERE e.kind = 'tool_call' AND t.name = 'shell:vcs'
  AND (etc.input LIKE '%git %' OR etc.input LIKE '%git\n%')
GROUP BY mode
ORDER BY count DESC;

-- 5. Test tool breakdown
SELECT
    CASE
        WHEN etc.input LIKE '%uv run pytest%' THEN 'pytest'
        WHEN etc.input LIKE '%pytest%' THEN 'pytest'
        ELSE 'other'
    END AS tool,
    COUNT(*) AS count
FROM events e
JOIN event_tool_call etc ON etc.event_id = e.id
JOIN tool_call_tags tct ON tct.tool_call_id = e.id
JOIN tags t ON t.id = tct.tag_id
WHERE e.kind = 'tool_call' AND t.name = 'shell:test'
GROUP BY tool
ORDER BY count DESC;
