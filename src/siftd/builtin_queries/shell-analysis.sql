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
        WHEN tc.input LIKE '%git %' OR tc.input LIKE '%git\n%' THEN 'git'
        WHEN tc.input LIKE '%yadm %' THEN 'yadm'
        WHEN tc.input LIKE '%gh %' THEN 'gh'
        ELSE 'other'
    END AS tool,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM tool_calls tc
JOIN tool_call_tags tct ON tct.tool_call_id = tc.id
JOIN tags t ON t.id = tct.tag_id
WHERE t.name = 'shell:vcs'
GROUP BY tool
ORDER BY count DESC;

-- 3. Git action distribution (subcommand after 'git')
SELECT
    CASE
        WHEN tc.input LIKE '%git add%' THEN 'add'
        WHEN tc.input LIKE '%git log%' THEN 'log'
        WHEN tc.input LIKE '%git status%' THEN 'status'
        WHEN tc.input LIKE '%git diff%' THEN 'diff'
        WHEN tc.input LIKE '%git commit%' THEN 'commit'
        WHEN tc.input LIKE '%git show%' THEN 'show'
        WHEN tc.input LIKE '%git checkout%' THEN 'checkout'
        WHEN tc.input LIKE '%git push%' THEN 'push'
        WHEN tc.input LIKE '%git branch%' THEN 'branch'
        WHEN tc.input LIKE '%git pull%' THEN 'pull'
        WHEN tc.input LIKE '%git merge%' THEN 'merge'
        WHEN tc.input LIKE '%git worktree%' THEN 'worktree'
        WHEN tc.input LIKE '%git stash%' THEN 'stash'
        ELSE 'other'
    END AS action,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM tool_calls tc
JOIN tool_call_tags tct ON tct.tool_call_id = tc.id
JOIN tags t ON t.id = tct.tag_id
WHERE t.name = 'shell:vcs'
  AND (tc.input LIKE '%git %' OR tc.input LIKE '%git\n%')
GROUP BY action
ORDER BY count DESC;

-- 4. Git read vs write classification
SELECT
    CASE
        WHEN tc.input LIKE '%git status%'
          OR tc.input LIKE '%git log%'
          OR tc.input LIKE '%git diff%'
          OR tc.input LIKE '%git show%'
          OR tc.input LIKE '%git branch%' THEN 'read'
        WHEN tc.input LIKE '%git add%'
          OR tc.input LIKE '%git commit%'
          OR tc.input LIKE '%git push%'
          OR tc.input LIKE '%git pull%'
          OR tc.input LIKE '%git merge%'
          OR tc.input LIKE '%git rebase%'
          OR tc.input LIKE '%git reset%' THEN 'write'
        ELSE 'other'
    END AS mode,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM tool_calls tc
JOIN tool_call_tags tct ON tct.tool_call_id = tc.id
JOIN tags t ON t.id = tct.tag_id
WHERE t.name = 'shell:vcs'
  AND (tc.input LIKE '%git %' OR tc.input LIKE '%git\n%')
GROUP BY mode
ORDER BY count DESC;

-- 5. Test tool breakdown
SELECT
    CASE
        WHEN tc.input LIKE '%uv run pytest%' THEN 'pytest'
        WHEN tc.input LIKE '%pytest%' THEN 'pytest'
        ELSE 'other'
    END AS tool,
    COUNT(*) AS count
FROM tool_calls tc
JOIN tool_call_tags tct ON tct.tool_call_id = tc.id
JOIN tags t ON t.id = tct.tag_id
WHERE t.name = 'shell:test'
GROUP BY tool
ORDER BY count DESC;

