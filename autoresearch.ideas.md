# Autoresearch Ideas

## Completed adapters
- ✅ vscode.py: 100% (extracted yield_conversation to SDK)
- ✅ codex_cli.py: 100% (normalizer, early tool call, default_location_source)
- ✅ opencode.py: 99.4% (L241 tool→None by design)
- ✅ gemini_cli.py: 97.5% (L254,259-261 hash-based project resolution)

## Remaining targets (37 uncovered lines, 98% overall)
- sdk.py: 16 miss (95%) — peek logic edges, seek path, scattered helpers
- claude_code.py: 5 miss (97%) — scattered parse edge cases
- registry.py: 4 miss (89%) — drop-in adapter loading error paths
- copilot_cli.py: 3 miss (98%) — platform branch (win32 L30), discover, started_at
- pi_agent.py: 2 miss (99%) — discover, can_handle non-.pi path
- aider.py: 1 miss (99%) — yield_conversation already handled guard
- _jsonl.py: 1 miss (95%) — single edge case

## Reusable patterns established
- `default_location_source(adapter_module, filename)` — test DEFAULT_LOCATIONS can_handle
- `yield_conversation()` SDK helper — eliminate dead empty-prompts guards
- Normalizer tests: zero I/O cost, high coverage per LOC
- Out-of-order timestamps to cover started_at first-assignment paths

## Uncoverable lines (accept)
- copilot_cli L30: `DEFAULT_LOCATIONS = ["~/AppData/Local/..."]` — win32 platform branch
- gemini_cli L254,259-261: hash-based project directory resolution
