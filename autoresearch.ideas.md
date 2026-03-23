# Autoresearch Ideas — Current State

## Recently completed (100%)

- `api/sync.py` (296/296)
- `cli/db.py` (398/398)
- `cli/search.py` no-embed lane (400/400)
- `cli/data.py` (674/674)
- `cli/install.py` (303/303)
- `cli/meta.py` (229/229)
- `cli/query.py` (287/287)
- `cli/peek.py` (214/214)
- `cli/tool_search.py` (157/157)
- `cli/upgrade.py` (135/135)
- `cli/export.py` (55/55)
- `cli/tags.py` (426/426)
- `cli/sessions.py` (50/50) — step-down complete (efficiency improved to 0.25)
- `output/format_registry.py` (52/52) — step-down complete (efficiency improved to 0.05)
- `output/terminal_fmt.py` (125/125) — step-down complete (efficiency returned to 0.04)
- `output/narrative.py` (139/139) — step-up to 100% then step-down efficiency pass (to 0.10)
- `api/resources.py` (69/69) — step-up to 100% plus step-down compression pass (to 0.42)
- `api/auth.py` (30/30) — timeout branch covered and step-down pass complete (to 0.34)
- `api/file_refs.py` (49/49) — guard branch covered and step-down passes complete (to 0.19)
- `api/search.py` (155/155) — step-up to 100% and initial step-down pass (to 0.18)
- `api/stats.py` (232/232) — edge coverage complete with focused step-down benchmark (to 0.05)
- `api/tags.py` (73/73) — wrapper branches covered and focused step-down benchmark (to 0.12)
- `api/migrations.py` (15/15) — wrapper coverage complete with step-down pass (to 0.40)
- `api/conversations.py` (424/424) — final edge branches covered (prompt-without-response and empty SQL result) with focused lane; follow-up step-down attempt was flat at metric floor.
- `api/merge.py` (134/134) — FK-violation rollback branch covered and step-down pass completed (to 0.05).
- `api/slice.py` (70/70) — missing-source + FK-guard branches covered with focused lane; step-down pass completed (to 0.07).
- `api/export.py` (66/66) — default-fidelity branch covered with focused lane; step-down attempt was flat at metric floor (discarded).
- `api/sessions.py` (17/17) — wrapper gap closed; step-down pass completed (to 0.17).
- `api/adapters.py` (30/30) — final entrypoint loop branch covered; focused lane currently at step-down 0.11.
- `api/__init__.py` (25/25) — unknown-symbol defensive AttributeError path covered.
- `adapters/_jsonl.py` (21/21) — string fast-path covered with focused edge test.
- `adapters/registry.py` (35/35) — config path-override branch covered; step-down pass to 0.08.
- `model_names.py` (41/41) — gemini unknown-variant fallback covered; step-down pass to 0.03.
- `adapters/aider.py` (156/156) — analytics location-match branch closed; step-down attempt hit flat metric floor.
- `adapters/claude_code.py` (147/147) — `_normalize_content` fallback branch closed with focused edge test.
- `adapters/opencode.py` (174/174) — `_part_to_content_block` tool-skip branch closed with focused edge test.
- `adapters/pi_agent.py` (154/154) — deterministic residual branches closed; focused step-down reached 0.02.
- `adapters/sdk.py` (328/328) — seek_last_lines binary-open OSError branch closed; lane now saturated.
- `tool_query.py` (80/80) — ToolQueryTerm.is_fielded property branch closed with focused edge test.
- `peek/reader.py` (162/162) — derived peek_scan wrapper invocation path covered; step-down pass to 0.02.
- `output/painted_bridge.py` (372/374) — cleanup attempt confirmed flat metric at current benchmark floor; remaining 2 lines still appear structurally unreachable (`if not parts` after unconditional header append).

## Pruned stale ideas

- Removed stale `cli/tags.py`, `cli/sessions.py`, `output/format_registry.py`, and `output/terminal_fmt.py` targets after saturation.
- Demoted `output/painted_bridge.py` from active target to near-saturated notes.
- Removed completed CLI sweep leftovers; keep only unsaturated post-CLI lanes.

## Highest ROI next targets

### 1) `doctor/checks/freelist.py`
- Single deterministic miss in MB-format branch for large freelist waste.
- Focused lane: `tests/test_doctor_freelist_edges.py`.

### 2) documented likely-unreachable branches (skip for now)
- `content/filters.py` L64–65: exception guard in `is_binary_content` appears structurally unreachable after `isinstance(content, str)` and `encode(..., errors='ignore')`.
- `backfill.py` L341–342: `new_hash == old_hash` path appears redundant after identity/no-change guard.

### 3) `output/painted_bridge.py` documentation/closure pass
- Remaining miss lines likely structurally unreachable; document and de-prioritize unless new evidence appears.

## Deferred but promising

- Reduce `tests/test_api_search_edges.py` LOC via shared hybrid stub fixtures; prior aggressive rewrite triggered early acceptance failure (`test_doctor.t`) during full-suite benchmark, so retry in smaller safe increments.

## De-prioritized

- `serve/*`, `embeddings/*` (marker/runtime heavy)
- `adapters/template.py` (example code)
- brittle terminal/TTY-only defensive branches unless they block practical milestones
