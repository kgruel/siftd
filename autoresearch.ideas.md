# Autoresearch Ideas

## Adapter Coverage Efficiency (current: ~4.73, baseline: TBD)

### High-value coverage targets (by uncovered stmts)
- sdk.py: 272 uncovered / 325 total — shared utilities, likely covers many adapters at once
- codex_cli.py: 173 uncovered / 194 total — large parser with tool handling
- vscode.py: 164 uncovered / 195 total — JSON/JSONL normalizer, peek
- opencode.py: 150 uncovered / 174 total — SQLite-based, needs fixture DB
- aider.py: 134 uncovered / 158 total — markdown parser with token counts
- gemini_cli.py: 133 uncovered / 158 total — tool/thinking block handling
- pi_agent.py: 133 uncovered / 154 total — JSONL session parser
- claude_code.py: 127 uncovered / 149 total — JSONL with subagent handling
- copilot_cli.py: 111 uncovered / 132 total — reasoning model usage parsing

### Strategy notes
- Focus on adapters with the most stmts first (sdk.py, codex_cli, vscode)
- Many adapters share sdk.py patterns — testing discover/peek on one covers sdk.py
- Look for multi-adapter test patterns that cover shared code paths efficiently
- Existing tests are ~704 LOC covering 271 lines — 2.6 LOC per covered line, room to improve ratio
- discover() and peek() methods are likely untested on most adapters — high coverage per test

## Previous storage ideas (archived)
- Migration test schemas are bulky (~350 LOC for 12 tests). Could share a common base schema dict
- Apply same metric to api/ (23% coverage, 1709 stmts)
