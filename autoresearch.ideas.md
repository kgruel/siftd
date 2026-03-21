# Autoresearch Ideas

## Per-adapter optimization targets (by uncovered stmts, post-split)

### Remaining ~92 uncovered lines
- vscode.py: 13 miss — JSONL replay edge cases, path traversal guards, workspace folder fallback
- opencode.py: 11 miss — sqlite error paths, JSON parse errors, pending tool status
- sdk.py: 16 miss — scattered edge cases in peek logic, small-file seek path
- codex_cli.py: 10 miss — _parse_block string/unknown, _get_or_create_response, normalizer edges
- gemini_cli.py: 10 miss — can_handle non-file, discover path, peek edges
- claude_code.py: 6 miss — scattered edge cases in parse
- copilot_cli.py: 4 miss — platform branch (win32), DEFAULT_LOCATIONS expansion
- pi_agent.py: 3 miss — can_handle when .pi not followed by agent/sessions
- registry.py: 4 miss — drop-in adapter loading error paths
- aider.py: 2 miss — analytics can_handle via DEFAULT_LOCATIONS expansion, empty session skip
- _jsonl.py: 1 miss — single edge case

### Strategy notes
- Normalizer tests (copilot, pi_agent) proved zero-time-cost and high-coverage — apply same to codex_cli
- Per-adapter files mean we can now target individual adapters without bloating others
- The `_fixture_source` helper is duplicated in codex_cli, gemini_cli, copilot_cli, pi_agent — could move to conftest
- Platform-specific branches (copilot_cli win32 line 30) are uncoverable on macOS — accept as dead code
- Most remaining gaps are error-handling branches requiring specific failure conditions

## Future targets
- Apply same metric to api/ (23% coverage, 1709 stmts)
- Apply same metric to storage/ (previous experiment reached 0.74 efficiency)
