# Autoresearch Ideas

## Completed adapters
- ✅ vscode.py: 100% (was 93%, +13 lines covered, extracted yield_conversation to SDK)
- ✅ opencode.py: 99.4% (was 89%, +18 lines covered, L241 tool→None by design)

## Remaining per-adapter targets (59 uncovered lines total)
- sdk.py: 16 miss (95%) — peek logic edges, small-file seek path, scattered helpers
- codex_cli.py: 14 miss (93%) — _parse_block string/unknown, normalizer edges
- gemini_cli.py: 10 miss (94%) — discover path, peek edges
- claude_code.py: 5 miss (97%) — scattered parse edge cases
- copilot_cli.py: 4 miss (97%) — platform branch (win32 uncoverable on macOS)
- registry.py: 4 miss (89%) — drop-in adapter loading error paths
- pi_agent.py: 3 miss (98%) — can_handle path edge
- _jsonl.py: 1 miss (95%) — single edge case
- aider.py: 1 miss (99%) — yield_conversation already handled the guard

## Strategy notes
- codex_cli has best ratio: 14 miss at only 52 LOC currently → lots of headroom
- sdk.py tests live in test_infra.py (142 LOC) — could compress
- Platform-specific branches (copilot_cli win32) are uncoverable on macOS
- yield_conversation extraction pattern could apply to opencode L241 (tool→None)
