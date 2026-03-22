# Autoresearch Ideas — Output Layer

## Current state: 78% overall (267 uncovered lines)

### Coverage by file
- ✅ theme.py: 100%
- ✅ validation.py: 100%
- ✅ formatters.py: 100% (empty)
- ✅ registry.py: 100% (empty)
- 🔶 format_registry.py: 92% (4 miss — select_format edge cases)
- 🔶 narrative.py: 91% (4 miss — MarkdownEmitter methods)
- 🔶 markdown_fmt.py: 87% (23 miss — render_detail, render_search modes)
- 🔶 terminal_fmt.py: 86% (17 miss — render_search modes)
- 🔶 painted_bridge.py: 77% (140 miss — tool presenters, detail/peek/follow)
- 🔶 json_fmt.py: 72% (18 miss — render_search, render_detail fallback)
- 🔶 common.py: 47% (61 miss — format_refs, print_refs_content, print_indented)

### High-ROI targets (pure logic, no I/O)
- [ ] common.py: fmt_tokens, fmt_workspace, fmt_ago, fmt_model, truncate_text, format_refs_annotation, print_refs_content — all pure functions, very testable
- [ ] json_fmt render_search: 3 modes (chunks, conversations, thread) — dict output, easy assertions
- [ ] json_fmt render_detail fallback: needs turns with narrative — use dataclass stubs
- [ ] markdown_fmt render_detail: header generation with metadata parts
- [ ] markdown_fmt render_search: 3 modes, string output
- [ ] narrative.py MarkdownEmitter: tool_content, tool_output, thinking — 4 lines

### Medium-ROI targets (need painted)
- [ ] painted_bridge tool presenters: _render_shell_execute_lines, _render_file_read_lines, etc. — test via render_narrative_block with fixture blocks
- [ ] terminal_fmt render_search: 3 modes — returns string, testable
- [ ] painted_bridge render_query_detail_block: needs ConversationDetail stub
- [ ] painted_bridge render_peek_detail_block: needs PeekSession stub
- [ ] painted_bridge render_follow_event_block: needs FollowEvent stub

### Patterns to apply
- Dataclass stubs for ConversationDetail, Turn, NarrativeBlock, etc.
- Direct MarkdownEmitter testing (zero I/O, high coverage per LOC)
- Dict assertion for json_fmt (no rendering needed)
- String assertion for markdown_fmt and terminal_fmt render_search
- Block-to-text extraction for painted_bridge (pattern from existing test_output_formats.py)

### Metric note
New metric: `miss + LOC×time/covered` — no edge zone rules needed.
Starting score ≈ 267. Target: <30 (high coverage, reasonable efficiency).
