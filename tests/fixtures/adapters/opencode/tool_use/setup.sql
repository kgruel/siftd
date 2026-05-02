-- OpenCode golden fixture: tool_use case
--
-- One session with a bash tool call and cost/cache metadata.
-- Timestamps are milliseconds since epoch (2024-03-10T14:00:00 UTC = 1710079200000).
-- Session ID: ses_golden_001

CREATE TABLE session (
    id TEXT,
    project_id TEXT,
    directory TEXT,
    title TEXT,
    version INTEGER,
    time_created INTEGER,
    time_updated INTEGER
);

CREATE TABLE message (
    id TEXT,
    session_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
);

CREATE TABLE part (
    id TEXT,
    message_id TEXT,
    session_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
);

-- Session: one workspace with a coding task
INSERT INTO session VALUES (
    'ses_golden_001',
    'proj_golden_001',
    '/test/workspace',
    'Run the tests',
    1,
    1710079200000,
    1710079260000
);

-- User message: prompt asking to run the test suite
INSERT INTO message VALUES (
    'm1',
    'ses_golden_001',
    1710079210000,
    1710079210000,
    '{"role": "user", "summary": {"title": "Run the tests"}}'
);

-- User prompt text part
INSERT INTO part VALUES (
    'p1',
    'm1',
    'ses_golden_001',
    1710079210000,
    1710079210000,
    '{"type": "text", "text": "Run the tests please"}'
);

-- Assistant message: response with bash tool invocation, cost, and cache metadata
INSERT INTO message VALUES (
    'm2',
    'ses_golden_001',
    1710079220000,
    1710079220000,
    '{"role": "assistant", "modelID": "claude-3-opus-20240229", "providerID": "anthropic", "cost": 0.025, "tokens": {"total": 680, "input": 500, "output": 120, "reasoning": 60, "cache": {"read": 50, "write": 10}}, "finish": "tool-calls"}'
);

-- Assistant text content part
INSERT INTO part VALUES (
    'p2',
    'm2',
    'ses_golden_001',
    1710079220000,
    1710079220000,
    '{"type": "text", "text": "I will run the tests for you."}'
);

-- Tool invocation part: bash tool call with completed status and output
INSERT INTO part VALUES (
    'p3',
    'm2',
    'ses_golden_001',
    1710079225000,
    1710079225000,
    '{"type": "tool", "callID": "c1", "tool": "bash", "state": {"status": "completed", "input": {"command": "pytest"}, "output": "5 passed, 0 failed", "time": {"start": 1710079225000, "end": 1710079228000}}}'
);
