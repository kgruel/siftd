# Adapter Research Reference

Research from tbd-v1 (`~/Code/tbd/docs/research/`) covers 10 CLI coding tools. Summary of what's available and how actionable each is for writing a drop-in adapter.

## Ready to Implement (full spec documented)

| Tool | Format | Location | Research File | Notes |
|------|--------|----------|---------------|-------|
| Cursor | SQLite | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | `providers/cursor.md` | Two-phase KV lookup, schema fragile across versions |
| Cline | JSON arrays | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/` | `providers/cline.md` | Task-centric, dual message formats (API + UI) |
| Goose | SQLite | `~/.local/share/goose/sessions/sessions.db` | `providers/goose.md` | Clean schema, multiple session types, accumulated tokens |
| OpenHands | Event-sourced JSON | `~/.openhands/conversations/` | `providers/openhands.md` | 21 action types, causality tracking, no token data |
| OpenCode | SQLite | `~/.local/share/opencode/opencode.db` | `providers/opencode.md` | Archived project, hierarchical sessions, file versioning |

## Needs More Research

| Tool | What's Known | Gap | Research File |
|------|-------------|-----|---------------|
| Aider | JSONL, `~/.aider/history/`, has cost data | No dedicated spec — would need to examine source or ccusage tool | `landscape.md` |
| GitHub Copilot | JSON, `~/.copilot/session-state/` | Minimal detail, no schema documented | `landscape.md`, `landscape-search.md` |

## Already Implemented in strata

| Tool | Adapter | Notes |
|------|---------|-------|
| Claude Code | `src/adapters/claude_code.py` | File dedup, cache tokens, 6 token types |
| Gemini CLI | `src/adapters/gemini_cli.py` | Session dedup, 6 token categories |
| Codex CLI | `src/adapters/codex_cli.py` | File dedup, conversation-only (no tokens/tools in format) |

## Cross-Provider Considerations

From `cross-provider-analysis.md`:
- Token type normalization varies significantly (Claude: 6 types, Gemini: 6 different types, Goose: 3, others: 2)
- Tool name normalization already handled by `src/adapters/claude_code.py` TOOL_ALIASES pattern
- Some tools track cost natively (Cline, Goose, Cursor) — could populate pricing data during ingest

## Priority Recommendation (from landscape.md)

1. **Goose** — SQLite source, clean schema, growing adoption
2. **Cline** — Large user base, good data richness
3. **Cursor** — Widely used, but schema fragility is a risk
4. **Aider** — High adoption, needs research first

All research files: `~/Code/tbd/docs/research/providers/`
Landscape overview: `~/Code/tbd/docs/research/landscape.md`
