# Plan: retro/hygiene-ux

## Current State

This plan covers the hygiene/UX backlog items listed in the task, verified against code on 2026-04-25. `REVIEW-BACKLOG.md` is not present in this worktree, so the task's verified item list is treated as the source of truth and cross-checked against live code. Scope is planning only; no implementation changes are included here.

## Triage

| Item | Impact | Effort | Files | Plan | Intentional facade defense? |
| --- | --- | --- | --- | --- | --- |
| R9 FTS parse errors | High | M | `src/siftd/storage/fts.py:148-170`, `:186-197`, `:272-296` | Default user search input to sanitized FTS5 tokens instead of raw `MATCH` syntax; add explicit raw mode. | No. User input crashing search is not an acceptable facade. |
| R15 duplicate workspace auto-merge | High | S | `src/siftd/doctor/checks/workspace_identity.py:37-40`, `src/siftd/cli/data.py:785-789` | Make duplicate finding informational/manual-only or remove `fix_available` for merge; keep backfill fix automatic. | No. A warning that can trigger destructive-ish merge is unsafe. |
| R16 path containment | Med | S | `src/siftd/peek/reader.py:311` | Replace `str.startswith()` containment with resolved `Path.is_relative_to()`. | No. This is correctness/security hygiene. |
| H10 swallowed delegation errors | Low | S | `src/siftd/serve/delegation.py:250-251` | Catch only expected delegation/network/serialization failures or log unexpected exceptions via existing logging/safecall conventions. | Partial. Fallback-to-local is intentional, but silent programming-error masking is not. |
| H22 symlink glob escape | Med | M | `src/siftd/plugin_discovery.py:98`, `src/siftd/adapters/sdk.py:70`, `src/siftd/peek/scanner.py:111` | Add per-call-site containment handling after glob discovery; do not introduce a new discovery layer. | Partial. Following symlinks may be useful for user-selected scan roots, but must be explicit/contained. |
| H24 doctor callback exceptions | Med | S | `src/siftd/doctor/runner.py:115-116` | Wrap `on_check_done()` so UI/progress callback failure does not fail checks. | No. Callback failure should be isolated. |
| H26 query-file reads bypass safecall | Med | S | `src/siftd/api/conversations.py:906`, `:980` | Use existing `safecall` patterns around `read_text()` and surface `QueryError`/structured errors. | No. User query files are external input. |
| H28 broad FTS exception catch | Med | S | `src/siftd/storage/fts.py:248-263` | Narrow to `sqlite3.OperationalError` for malformed FTS syntax and self-constructed OR failures; let programming errors surface. | No. It currently hides real defects. |
| H2 table interpolation | Med | S | `src/siftd/storage/queries.py:528` | Add an allowlist for known table names before interpolating `table_name`; preserve current owner-specific branches. | Partial. Internal helper may only get trusted constants today, but an allowlist is cheap and clearer. |
| H3 column-list interpolation | Med | S | `src/siftd/storage/sqlite.py:795`, `:816`, `:833` | Validate `kwargs` keys against per-table allowed columns before building insert column lists. | Partial. Current callers likely pass internal kwargs, but helper API invites misuse. |
| H11 short-token search loss | Med | S | `src/siftd/storage/fts.py:177` | Stop dropping one- and two-character tokens in sanitized search/OR rewrite; quote them and rely on FTS tokenization. | No. Searching for Go, R, C is valid UX. |
| H13 Gemini hash lookup cost | Low | S | `src/siftd/adapters/gemini_cli.py:266-297` | Memoize project-hash resolution for one ingest process; avoid repeated directory walks. | Possible. Correctness is unaffected, but performance degradation is real and easy to fix. |
| H14 Aider analytics discovered but unparsed | Med | M | `src/siftd/adapters/aider.py:61-68`, `:96-102` | Either parse `analytics.jsonl` into conversations/events or stop discovering it until supported; prefer parsing if fixture shape is known. | Yes, if analytics discovery was intentionally reserved, but current discovery creates a misleading no-op. |
| H16 adapter signature validation | Med | M | `src/siftd/adapters/validation.py:63-71`, `src/siftd/doctor/checks/drop_ins_valid.py:24-33` | Extend validation to check callable signatures for `discover(locations=...)`, `can_handle(source)`, and `parse(source)`. | No. Drop-ins are external extension points. |
| H19 internal IDs in JSON | Med | M | `src/siftd/output/json_fmt.py:157-162`, `:175`, `src/siftd/serialization/serve_fmt.py:50-59` | Pick stance (a): hide internal ULIDs by default and expose them only with `--debug-ids`/debug fidelity. | Yes, if JSON was deliberately an internal/debug format; otherwise document the breaking change. |
| H20 missing uvicorn dependency check | High | S | `src/siftd/serve/__init__.py:10-17`, `src/siftd/cli/serve.py:51` | Make `require_serve()` import-check both `litestar` and `uvicorn`, with install guidance before `cli/serve.py` imports uvicorn. | No. Missing optional dependency produces a confusing runtime failure. |

## Bundling

1. **FTS UX and robustness: R9 + H11 + H28**
   Touches `src/siftd/storage/fts.py` and focused tests. This should be one PR because tokenization, OR rewrite, and exception handling are the same behavioral surface.

2. **Doctor and external-file safety: R15 + H24 + H26**
   Touches doctor runner/checks plus query-file API. These all convert unsafe automation or external-input failures into explicit, controlled outcomes.

3. **Filesystem containment: R16 + H22**
   Touches `peek/reader.py`, `peek/scanner.py`, `plugin_discovery.py`, and `adapters/sdk.py`. One PR can establish consistent `resolve()` plus `is_relative_to()` decisions without a shared abstraction.

4. **SQL helper hygiene: H2 + H3**
   Touches `storage/queries.py` and `storage/sqlite.py`. Both are identifier interpolation hardening and should ship together with narrow unit tests.

5. **Adapter hygiene: H13 + H14 + H16**
   Touches Gemini, Aider, adapter validation, and drop-in doctor tests. This keeps adapter-contract fixes together while avoiding search/serve churn.

6. **Output/serve optional dependency UX: H19 + H20 + H10**
   Touches JSON/serve serialization and serve dependency/delegation fallback. These are user-facing serve/API surface items; H10 is small and fits here because it affects serve delegation behavior.

## R9 Design: Tokenize and Quote FTS5 by Default

Add a helper in `src/siftd/storage/fts.py`, near `_fts5_or_rewrite()`:

```python
@dataclass(frozen=True)
class SanitizedFts5Query:
    fts_query: str | None
    tokens: list[str]
    raw: bool = False

def sanitize_fts5_query(query: str, *, raw: bool = False, operator: Literal["and", "or"] = "and") -> SanitizedFts5Query:
    ...
```

The default path tokenizes user input with a simple regex matching FTS terms, strips FTS operators from control position, and emits quoted terms. Examples:

| Input | Default sanitized query |
| --- | --- |
| `foo bar` | `"foo" "bar"` |
| `Go R C` | `"Go" "R" "C"` |
| `NOT crash` | `"NOT" "crash"` |
| `foo* "bar` | `"foo" "bar"` |
| empty or punctuation-only | `None` |

Raw mode should preserve current FTS5 syntax behavior for expert callers. Put opt-in raw mode at command/API boundaries, not inside storage only:

- CLI: add `--raw-fts` to query/search commands that pass keyword FTS into `fts5_recall_details()` or `search_content()`.
- API: add a boolean `raw_fts: bool = False` parameter to the existing API operation dataclass/function path that eventually calls storage FTS. Do not create a new architectural layer.
- Prefix: do not use a magic prefix such as `fts:` as the first implementation; flags/API parameters are discoverable and less likely to collide with real search terms.

`fts5_recall_details()` should call `sanitize_fts5_query(query, raw=raw_fts, operator="and")` for phase 1. If phase 1 has fewer than `min_and_hits`, reuse the same token list and build OR form with `operator="or"`. This replaces `_fts5_or_rewrite()`'s current `len(t) >= 3` filtering at `storage/fts.py:177`, so short language/tool names survive. In raw mode, phase 1 uses the raw query, while phase 2 may either skip OR rewrite or tokenize the raw string as fallback; prefer tokenized fallback so malformed expert syntax still degrades to useful search instead of no results.

Narrow exception handling in the same PR: catch `sqlite3.OperationalError` for malformed FTS at `storage/fts.py:248-263`; do not catch broad `Exception`.

## H19 Stance: Hide Internal IDs by Default

Pick stance **(a): hide internal IDs by default + `--debug-ids`**.

Rationale: ULIDs are storage implementation details and leak cross-output coupling. Default JSON should expose stable, user-meaningful fields (`workspace`, timestamps, source path/display data, scores, snippets) and only include `conversation_id`, `chunk_id`, and `source_ids` when explicitly requested.

Implementation plan:

- Add a debug ID option at output/API boundary, reusing existing fidelity/dataclass patterns if present.
- In `src/siftd/output/json_fmt.py:157-162`, omit `chunk_id`, `conversation_id`, and `source_ids` unless debug IDs are enabled.
- In `src/siftd/serialization/serve_fmt.py:50-59`, apply the same rule to serve JSON.
- Preserve internal IDs in terminal links or command inputs only where a user must address an entity by ID. Do not remove IDs from internal Python APIs.
- Add a CHANGELOG deprecation/breaking note because JSON consumers may currently depend on IDs.

## H22 Per-Call-Site Symlink Decision

- `src/siftd/plugin_discovery.py:98`: **reject symlinked plugin files by default**. Drop-in plugin directories execute Python code; allowing symlink escape from configured plugin dirs expands the trust boundary. Resolve each matched `.py`, require `resolved.is_relative_to(path.resolve())`, and skip symlink escapes with a warning.

- `src/siftd/adapters/sdk.py:70`: **resolve and contain by default** for adapter discovery helpers. For default locations and user-provided scan roots, only yield files whose resolved path stays under the resolved base. This prevents broad `**` globs from escaping configured roots while still allowing symlinks that point within the tree.

- `src/siftd/peek/scanner.py:111`: **resolve and contain**. Peek is a live/recent-session scanner; it should not cross out of adapter `DEFAULT_LOCATIONS` through symlinks. Skip out-of-tree matches silently or with debug logging to avoid noisy live scans.

Pair this with R16 in `src/siftd/peek/reader.py:311`: use `file_resolved.is_relative_to(base)` after both paths are resolved.

## Sequencing

1. Ship **FTS UX and robustness** first. R9 is high-impact because normal search input can crash, and H11/H28 are natural same-file fixes. Add tests for punctuation, quotes, `NOT`, `*`, and one-letter/two-letter tokens.

2. Ship **Doctor and external-file safety** second. R15 should land early because `doctor fix` can perform an unintended merge. This PR needs a CHANGELOG note if `siftd doctor fix` behavior changes from auto-merge to manual-only for duplicate workspaces.

3. Ship **Filesystem containment** third. This is security/robustness hardening and can be reviewed independently with symlink fixtures.

4. Ship **SQL helper hygiene** fourth. Low behavior change, but useful to keep the storage hardening isolated.

5. Ship **Output/serve optional dependency UX** fifth. H20 is straightforward; H19 may be user-visible. Include a CHANGELOG deprecation/breaking note for JSON ID visibility and document `--debug-ids`.

6. Ship **Adapter hygiene** last. H14 may need fixture research for Aider analytics shape, so keep it off the critical path for crash/safety fixes.

## Verification Plan

Each PR should run `./dev check`. Additional targeted checks:

- FTS PR: unit tests around `sanitize_fts5_query()`, `fts5_recall_details()`, and `search_content()` malformed input behavior.
- Doctor PR: tests that duplicate workspace findings do not register an automatic merge fix, and callback failures do not abort check execution.
- Filesystem PR: symlink fixtures for plugin discovery, adapter SDK discovery, peek scanner, and peek reader containment.
- SQL PR: tests for allowed identifiers and rejection of unexpected table/column names.
- Adapter PR: fixture for Gemini hash memoization and Aider analytics behavior; signature validation tests for bad drop-ins.
- Output/serve PR: JSON snapshots with and without debug IDs; serve dependency test where `uvicorn` is absent.

## Out of Scope

- No new search architecture, parser package, or query DSL. R9 should stay in `storage/fts.py` plus existing CLI/API plumbing.
- No migration away from ULIDs internally. H19 only changes default serialized output.
- No broad plugin sandboxing. H22 only constrains filesystem discovery boundaries; executing trusted drop-ins remains the current model.
- No automatic workspace merge redesign. R15 should make unsafe auto-merge manual-only now; a future merge review UI/flow is separate.
- No full Aider product analytics model unless H14 fixtures show a stable, parseable format. If analytics JSONL is volatile or not conversation-like, the implementation PR may choose to stop discovering it and document that decision.
