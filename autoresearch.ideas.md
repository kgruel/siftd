# Autoresearch Ideas

## Completed adapters (all 97%+)
- ✅ vscode.py: 100%
- ✅ codex_cli.py: 100%
- ✅ validation.py: 100%
- ✅ opencode.py: 99% (L241 tool→None by design)
- ✅ claude_code.py: 99% (L279 _normalize_content fallback)
- ✅ aider.py: 99%
- ✅ pi_agent.py: 99%
- ✅ copilot_cli.py: 98% (L30 win32 platform)
- ✅ gemini_cli.py: 97% (L254,259-261 hash resolution)
- ✅ _jsonl.py: 95% (1 miss)

## Remaining targets (33 uncovered, 98% overall)
- **sdk.py: 16 miss (95%)** — biggest target, tested via test_infra.py (142 LOC)
  - peek logic: exchange assembly, scan edge cases
  - seek_last_lines: small-file branch
  - NormalizedRecord/iter_jsonl edges
- **registry.py: 4 miss (89%)** — drop-in adapter loading with validate errors
  - Lowest % adapter, small file (35 stmts) — could get to 100% cheaply

## Reusable patterns
- `default_location_source()` — test DEFAULT_LOCATIONS can_handle (in conftest)
- `yield_conversation()` — SDK helper eliminating dead guards
- Normalizer tests: zero I/O, high coverage per LOC
- Out-of-order timestamps for started_at first-assignment paths
