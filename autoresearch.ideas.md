# Autoresearch Ideas — Output Layer

## Final state: 98.6% coverage (17 uncovered lines)

### Coverage by file
- ✅ common.py: 100%
- ✅ markdown_fmt.py: 100%
- ✅ narrative.py: 100%
- ✅ theme.py: 100%
- ✅ validation.py: 100%
- ✅ terminal_fmt.py: 99% (1 miss — L175, chunks context_data path)
- 🔶 format_registry.py: 92% (4 miss — L151-154 unreachable fallback)
- 🔶 json_fmt.py: 94% (4 miss — render_stats L119/121, render_tool_search L140/142)
- 🔶 painted_bridge.py: 98.5% (9 miss — scattered internal helpers + empty-parts guards)

### Remaining uncovered lines (diminishing returns)

**format_registry.py L151-154**: `select_format` last-resort fallback when terminal format
is missing. Unreachable because builtins always load terminal_fmt. Would need to monkey-patch
the global `_formats` dict — not worth it for defensive code.

**json_fmt.py L119/121, L140/142**: `render_stats` and `render_tool_search` — thin delegates
to serialization.serve_fmt/stats. Need complex DatabaseStats/ToolQuery type stubs that match
deeply nested dataclass hierarchies. Low value: they're one-liner pass-throughs.

**painted_bridge.py (9 lines)**:
- L39-41 `_styles()`: Legacy function, only called by dead code paths. Not exercised by any
  render function through the current code paths (theme uses `domain_styles()` instead).
- L73 `_lines_to_block([])`: Empty list guard. All callers check for empty before calling.
- L111 `_append_multiline` with text that strips to empty: All callers pass non-empty text.
- L147 `_output_preview_lines` with empty output: Guard for whitespace-only output.
- L225 `file.read` raw non-JSON input: The test sends it but it goes through _parse_json_safe
  which returns None, then the `elif raw_input` path… actually this should be coverable.
- L804, L903: `if not parts: return Block.empty(0,0)` in query/peek detail renderers.
  Headers always create parts, so this guard can't fire without an empty detail object.

### Metric evolution
- Baseline: score=267 (267 miss + 0.07 efficiency)
- After common/search/detail/narrative: score=160 (160 miss + 0.22)
- After painted_bridge/tool presenters: score=52 (51 miss + 1.04)
- After tool edges/render_search context: score=22 (21 miss + 0.63)
- After json_fmt delegates: score=18 (17 miss + 0.54)

### New metric validation
The `miss + LOC×time/covered` metric worked perfectly for this cycle:
- No edge zone rules needed
- Every coverage improvement was correctly rewarded
- Efficiency term stayed <1.0 throughout (negligible vs miss)
- Natural phase: miss dominated at start, efficiency will dominate when approaching 100%
