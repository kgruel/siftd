# tests

The suite mirrors `src/siftd/`: a test file lives beside the layer it exercises
(`test_storage.py`, `tests/adapters/`, `tests/cli/`) and most tests are unit or
behavioral tests that run in the default lane. Optional-dependency and
long-running tests are gated by pytest markers so the base lane stays fast and
dependency-light. The generated per-directory rollup and file tables are below
the markers; this preamble is the map of *lanes* — which tests run where, and
under which command.

## Responsibility map

Lanes are defined by three markers (declared in `pyproject.toml`): `embeddings`,
`serve`, and `slow`. "Base" means unmarked — the default run. A marked test is
usually tagged at module scope with `pytestmark = pytest.mark.<lane>` and guards
its optional import with `pytest.importorskip(...)`. **A lane no CI job runs is a
test that doesn't exist**, so the CI column below is load-bearing: it is the
truth from `.github/workflows/ci.yml`, not an aspiration.

| Area | Covers | Lane / marker | Local command | CI job |
|------|--------|---------------|---------------|--------|
| `tests/` root (`test_*.py`) | Unit + behavioral tests for api, storage, output, domain, search, sync, peek, ingestion, serialization, config, cost/rollup | base (unmarked) | `./dev test` | `test` (3.12/3.13/3.14) |
| `tests/adapters/` | Per-adapter `can_handle` / `parse` / `discover` contracts + golden-fixture parity | base | `./dev test` | `test` |
| `tests/cli/` | Argparse-layer parsing and command behavior (exercise the parser, not `_command(_args(...))`) | base | `./dev test` | `test` |
| `tests/architecture/` | Boundary + fitness rules: import layering, hard-rule static analysis, CLI contracts, CSP fitness (T1/T2) | base | `./dev test` | `test` |
| `tests/snapshots/` | syrupy snapshots of `--help` output, stored per Python version | base | `./dev test` | `test` |
| `tests/acceptance/` (`*.t`) | End-to-end CLI transcripts via pytest-prysk (cram-style) | base | `./dev test` | `test` |
| Embeddings tests (across `tests/`, `mark.embeddings`) | Semantic search against a real fastembed/remote backend; the local ONNX stack | `embeddings` | `./dev test-embed`, `./dev test-all` | `test-with-embeddings` (3.12) |
| Serve tests (across `tests/`, `mark.serve`) | HTTP routes, auth, delegation wire parity, Swiss UI renderers, e2e TestClient smoke | `serve` | `./dev test-serve`, `./dev test-all` | `test-with-serve` (3.12) |
| Slow tests (`mark.slow`) | Tests >10s: real-subprocess sync e2e and similar | `slow` | `./dev test-slow` | `test-slow` — **release only** (`workflow_call`), not PR CI |
| `tests/browser_smoke/` (`smoke.py`) | T3 real-browser CSP smoke: headless Chromium over CDP | none — standalone script | `./dev browser-smoke` | **none** — no CI job runs it |

Two caveats worth internalizing. First, the `slow` lane runs only when the
publish workflow calls CI (`if: github.event_name == 'workflow_call'`), so a
regression it would catch will not surface on a pull request — run `./dev
test-slow` yourself before tagging a release. Second, the browser smoke is a
standalone Python script, not a pytest marker, and nothing in `ci.yml` invokes
it; it is a manual pre-merge check for serve UI / CSP work
(`docs/guides/serve-browser-testing.md`). `tests/architecture/test_csp_fitness.py`
holds the T1/T2 CSP fitness functions that *do* run in CI (they `importorskip`
litestar, so they execute in the serve/embed installs and skip in the base
lane).

`./dev check` (the CI equivalent) runs lint plus the base lane and, as its last
step, `./dev docs --check`. It does **not** run the embeddings, serve, slow, or
browser lanes — run those explicitly (or `./dev test-all` for embeddings +
serve) when your change touches them.

## Where a new test goes

Mirror the source. A change to `src/siftd/storage/` gets a case in
`test_storage.py`; a new adapter gets `tests/adapters/test_<adapter>.py` plus a
golden fixture; a CLI flag gets a test in `tests/cli/` that goes through the
argparse layer. Cross-cutting rules that must hold regardless of feature —
import direction, layer boundaries, "no direct storage import from the CLI" —
belong in `tests/architecture/` as a fitness function, not scattered across unit
tests. Genuinely end-to-end flows that drive the built binary or a real
subprocess go in `tests/acceptance/` (prysk `.t` transcripts) or an
`e2e`-flavored module. If your test needs an optional dependency
(fastembed, litestar) tag the module with the matching marker and
`importorskip` the import, so the base lane stays green without it.

## Load-bearing conventions

- **Fakes over mocks.** Shared test doubles live in `tests/fakes/` (e.g.
  `FakeSSH` for the sync transport); fixtures — golden adapter payloads, schema
  snapshots, minimal per-adapter logs — live in `tests/fixtures/`. Prefer a fake
  and a behavioral assertion over patching internals; the architecture lane
  guards the direction of dependencies, not the shape of your mocks.
- **Snapshot policy.** Help-output snapshots are stored per Python version
  (`__snapshots__/pyXY/`) because argparse wraps text differently across
  versions, and CI runs the full 3.12/3.13/3.14 matrix — so every version's
  snapshot must be present. Update them deliberately after intentional help
  changes; see [docs/guides/snapshot-policy.md](../docs/guides/snapshot-policy.md).
- **xdist safety.** The suite runs under `pytest-xdist` (`-n auto`). Never
  `monkeypatch` `sys.stdout` / `sys.stderr` — workers share process stdio and it
  races capture. Use `capsys`/`capfd` or a callback/`file=` parameter instead
  (see the note at the top of `conftest.py`). Every test is hard-isolated from
  the real database by the autouse `_sandbox_db_home` fixture.
- **Exercise the real edge.** CLI tests should go through argparse, not call the
  command function directly; route tests that must catch wire-contract drift use
  Litestar's `TestClient` end-to-end rather than calling handlers via `.fn()` —
  the pattern in `test_serve_e2e_smoke.py`. Unit tests catch logic bugs; these
  catch contract bugs.

The tables below are generated by `./dev docs` from test file docstrings and
counts — do not edit them by hand, and give each test module a one-line
docstring so its row is meaningful.

<!-- gen:begin tests -->
<sub>generated from test file docstrings — run <code>./dev docs</code></sub>

### Rollup

| Directory | Test files | Test functions |
|-----------|------------|----------------|
| `tests/` | 184 | 3169 |
| `tests/adapters/` | 19 | 161 |
| `tests/architecture/` | 5 | 49 |
| `tests/cli/` | 28 | 608 |
| `tests/snapshots/` | 1 | 5 |

### `tests/`

| File | Tests | Summary |
|------|-------|---------|
| [test_adapter_health.py](test_adapter_health.py) | 22 | Tests for adapter-health warnings: zero-discovery and drop-in import failures. |
| [test_api.py](test_api.py) | 114 | Tests for the public API module. |
| [test_api_adapters_edges.py](test_api_adapters_edges.py) | 2 | — |
| [test_api_backfill.py](test_api_backfill.py) | 5 | Tests for siftd.api.backfill. |
| [test_api_conversations_edges.py](test_api_conversations_edges.py) | 2 | — |
| [test_api_doctor.py](test_api_doctor.py) | 6 | Tests for siftd.api.doctor — wrapper re-exports from siftd.doctor. |
| [test_api_events.py](test_api_events.py) | 22 | Phase 4: tests for the event detail surface. |
| [test_api_export_edges.py](test_api_export_edges.py) | 1 | — |
| [test_api_ingest.py](test_api_ingest.py) | 6 | Tests for siftd.api.ingest. |
| [test_api_init_edges.py](test_api_init_edges.py) | 1 | — |
| [test_api_merge_edges.py](test_api_merge_edges.py) | 1 | — |
| [test_api_migrations_edges.py](test_api_migrations_edges.py) | 2 | — |
| [test_api_search_capture.py](test_api_search_capture.py) | 7 | Search-log capture at the api/search.py::search_view Operation (S2). |
| [test_api_search_edges.py](test_api_search_edges.py) | 11 | — |
| [test_api_search_opens.py](test_api_search_opens.py) | 8 | Opened-signal linkage at api/conversations.py::get_conversation (S3). |
| [test_api_sessions_edges.py](test_api_sessions_edges.py) | 1 | — |
| [test_api_slice_edges.py](test_api_slice_edges.py) | 2 | — |
| [test_api_stats_edges.py](test_api_stats_edges.py) | 2 | — |
| [test_api_tags_edges.py](test_api_tags_edges.py) | 2 | — |
| [test_api_tags_mutation.py](test_api_tags_mutation.py) | 13 | — |
| [test_attributes.py](test_attributes.py) | 6 | Tests for storage/attributes.py — set_attribute / get_attributes and downstream consumers. |
| [test_auth.py](test_auth.py) | 4 | Tests for siftd.api.auth — token acquisition. |
| [test_auto_index_hook.py](test_auto_index_hook.py) | 22 | Tests for the post-ingest auto-index hook (base lane — collaborators stubbed). |
| [test_auto_index_integration.py](test_auto_index_integration.py) | 2 | Real-backend integration for the post-ingest auto-index hook (embed lane — fastembed). |
| [test_backfill.py](test_backfill.py) | 28 | Tests for siftd.backfill module. |
| [test_blobs.py](test_blobs.py) | 0 | Blob storage tests — now in test_storage.py. |
| [test_builtin_harness_stats.py](test_builtin_harness_stats.py) | 1 | Correctness guard for the harness-stats builtin query (I20). |
| [test_caveats.py](test_caveats.py) | 120 | Tests for the caveats producer registry and dispatch threading. |
| [test_caveats_wire.py](test_caveats_wire.py) | 6 | I5 — caveat round-trip across the delegation wire. |
| [test_chunker.py](test_chunker.py) | 16 | Tests for token-aware chunking (schema-v2: estimator-decoupled, widened source_ids). |
| [test_cli_query.py](test_cli_query.py) | 6 | Tests for siftd query CLI error handling. |
| [test_config.py](test_config.py) | 66 | Tests for config module. |
| [test_content_filters.py](test_content_filters.py) | 35 | Tests for binary content filtering. |
| [test_credentials.py](test_credentials.py) | 31 | Tests for client-side token acquisition (src/siftd/credentials.py). |
| [test_date_parsing.py](test_date_parsing.py) | 9 | Tests for relative date parsing in CLI. |
| [test_debug_ids.py](test_debug_ids.py) | 13 | Phase 2: chunk IDs are now default-on in JSON. |
| [test_derivative.py](test_derivative.py) | 20 | Tests for derivative conversation detection, tagging, and backfill. |
| [test_derived_tier_write_paths.py](test_derived_tier_write_paths.py) | 4 | The derived tier (usage_by_conv_model / conversation_stats) is an invariant: |
| [test_dispatch.py](test_dispatch.py) | 6 | Tests for siftd.api.dispatch — Operation IR and execution. |
| [test_doctor.py](test_doctor.py) | 117 | Tests for the doctor module. |
| [test_doctor_freelist_edges.py](test_doctor_freelist_edges.py) | 1 | — |
| [test_doctor_view.py](test_doctor_view.py) | 12 | Tests for the painted doctor progress view (siftd/doctor/view.py). |
| [test_embeddings.py](test_embeddings.py) | 20 | Tests for the embeddings subsystem. |
| [test_embeddings_availability.py](test_embeddings_availability.py) | 14 | Embedding availability — status-driven (config/installed), not reachability. |
| [test_embeddings_base_edges.py](test_embeddings_base_edges.py) | 21 | Deterministic, config-driven backend resolution (base lane — no fastembed). |
| [test_embeddings_fastembed_backend_edges.py](test_embeddings_fastembed_backend_edges.py) | 3 | — |
| [test_embeddings_indexer_edges.py](test_embeddings_indexer_edges.py) | 20 | Edge tests for the schema-v2 index lifecycle (base lane — fake backend, no fastembed). |
| [test_embeddings_presets.py](test_embeddings_presets.py) | 9 | Embedding preset reference data — loading, per-provider fields, validation. |
| [test_embeddings_remote_edges.py](test_embeddings_remote_edges.py) | 19 | RemoteBackend edge cases — fake httpx transport, no network (base lane). |
| [test_embeddings_storage.py](test_embeddings_storage.py) | 15 | Tests for embeddings storage. |
| [test_event_tool_call_triggers.py](test_event_tool_call_triggers.py) | 8 | Tests for tr_event_tool_call_* blob reference-count triggers. |
| [test_events_roundtrip.py](test_events_roundtrip.py) | 13 | Roundtrip tests for the polymorphic events writers and readers (schema v4). |
| [test_exclude_active.py](test_exclude_active.py) | 6 | Tests for active session exclusion from search results. |
| [test_export.py](test_export.py) | 40 | Tests for the export API and formatter-driven rendering. |
| [test_export_elements.py](test_export_elements.py) | 7 | WS6: element export (siftd export --view elements --tag X). |
| [test_file_refs.py](test_file_refs.py) | 3 | — |
| [test_findability_review.py](test_findability_review.py) | 6 | — |
| [test_follow.py](test_follow.py) | 21 | Tests for siftd.peek.follow — live session following utilities. |
| [test_formatters.py](test_formatters.py) | 44 | Tests for output formatters and format registry. |
| [test_fts_h1.py](test_fts_h1.py) | 37 | Tests for H1: R9 (FTS5 sanitization), H11 (short tokens), H28 (exception narrowing). |
| [test_fts_rebuild_on_push.py](test_fts_rebuild_on_push.py) | 3 | AC test for ST-3b: FTS rebuild on push. |
| [test_gen_docs_cli.py](test_gen_docs_cli.py) | 4 | Tests for the cli target of scripts/gen_docs.py (parser-introspection generator). |
| [test_gen_docs_readmes.py](test_gen_docs_readmes.py) | 12 | Tests for the readmes target of scripts/gen_docs.py (marker engine + strict mode). |
| [test_git.py](test_git.py) | 35 | Tests for git utilities and workspace identity. |
| [test_gutter.py](test_gutter.py) | 10 | Tests for siftd.output.gutter — the grain-gutter taxonomy + the rail it draws. |
| [test_html_dashboard.py](test_html_dashboard.py) | 11 | Unit tests for the Swiss Stats dashboard renderer (output/html_fmt.render_dashboard). |
| [test_html_folio.py](test_html_folio.py) | 45 | Unit tests for the Swiss transcript folio renderer (output/html_fmt.render_folio). |
| [test_html_sessions.py](test_html_sessions.py) | 7 | Unit tests for the Swiss Sessions renderer (output/html_fmt.render_sessions). |
| [test_html_tags.py](test_html_tags.py) | 3 | Unit tests for the Swiss Tags renderer (output/html_fmt.render_tags). |
| [test_html_workspaces.py](test_html_workspaces.py) | 7 | Unit tests for the Swiss Workspaces renderer (output/html_fmt.render_workspaces). |
| [test_id_resolution.py](test_id_resolution.py) | 25 | Tests for converged ID resolver (AmbiguousPrefix detection) and short_id bump. |
| [test_inbox.py](test_inbox.py) | 17 | Tests for siftd.api.inbox — staged receive and inbox processing. |
| [test_ingest_session_multi.py](test_ingest_session_multi.py) | 4 | Regression tests for C01 (comprehensive-review 2026-05-28). |
| [test_ingest_vocab_cache_rollback.py](test_ingest_vocab_cache_rollback.py) | 1 | Regression test for C02 (comprehensive-review 2026-05-28). |
| [test_ingestion.py](test_ingestion.py) | 39 | Tests for ingestion orchestration utility functions. |
| [test_integration.py](test_integration.py) | 14 | End-to-end integration tests. |
| [test_listing.py](test_listing.py) | 26 | Tests for siftd.output.listing — the aligned key:value report atom. |
| [test_live.py](test_live.py) | 23 | Tests for the live-render policy (output/live.py). |
| [test_live_tagging.py](test_live_tagging.py) | 12 | Integration tests for live session tagging flow. |
| [test_markdown_render.py](test_markdown_render.py) | 21 | Tests for terminal markdown rendering of transcript bodies. |
| [test_math.py](test_math.py) | 9 | Tests for siftd.math module. |
| [test_merge.py](test_merge.py) | 35 | Tests for siftd db merge — importing a slice into the main database. |
| [test_merge_blob_gc.py](test_merge_blob_gc.py) | 2 | D2 — orphan content_blob GC on merge. |
| [test_merge_owner_scope.py](test_merge_owner_scope.py) | 3 | Multi-tenant write-IDOR guard for the merge path (S0/S1/D1). |
| [test_migrations.py](test_migrations.py) | 67 | Tests for siftd storage migration paths. |
| [test_mmr.py](test_mmr.py) | 12 | Tests for MMR diversity reranking in search.py. |
| [test_mmr_focus.py](test_mmr_focus.py) | 1 | — |
| [test_model_names.py](test_model_names.py) | 4 | Tests for model name parsing. |
| [test_model_names_edges.py](test_model_names_edges.py) | 1 | — |
| [test_narrative.py](test_narrative.py) | 12 | Tests for siftd.serialization.narrative + conversations + json_fmt + format_registry. |
| [test_normalizers.py](test_normalizers.py) | 4 | Cross-format normalizer validation tests. |
| [test_op_route_parity.py](test_op_route_parity.py) | 7 | Contract tests: CLI Operation params must be accepted by their serve route. |
| [test_output_common.py](test_output_common.py) | 32 | Tests for siftd.output.common — pure formatting helpers. |
| [test_output_format_registry.py](test_output_format_registry.py) | 1 | — |
| [test_output_formats.py](test_output_formats.py) | 129 | Tests for output format rendering: lists, search, detail, narrative. |
| [test_output_help.py](test_output_help.py) | 23 | Unit tests for siftd.output.help — the one help grammar. |
| [test_output_narrative.py](test_output_narrative.py) | 5 | — |
| [test_output_painted_bridge_edges.py](test_output_painted_bridge_edges.py) | 2 | — |
| [test_output_terminal_fmt.py](test_output_terminal_fmt.py) | 1 | — |
| [test_painted_bridge.py](test_painted_bridge.py) | 1 | — |
| [test_peek.py](test_peek.py) | 45 | Tests for the peek module. |
| [test_peek_follow.py](test_peek_follow.py) | 35 | Tests for peek follow mode: parsing, rendering, and hint extraction. |
| [test_plugin_discovery.py](test_plugin_discovery.py) | 30 | Tests for siftd.plugin_discovery module. |
| [test_preflight.py](test_preflight.py) | 10 | Unit tests for siftd.api.database preflight functions. |
| [test_pricing.py](test_pricing.py) | 10 | v11 pricing-as-reference: the pricing table is a projection of the version-controlled |
| [test_progress.py](test_progress.py) | 5 | Tests for the ProgressEvent contract (domain/progress.py). |
| [test_progress_view.py](test_progress_view.py) | 20 | Tests for the generic ProgressEvent consumer (output/progress_view.py). |
| [test_push_error_mapping.py](test_push_error_mapping.py) | 5 | I1 — push failures surface a structured, actionable error (client side). |
| [test_push_windowing.py](test_push_windowing.py) | 27 | Tests for push windowing (feat/push-windowing). |
| [test_push_windowing_floor.py](test_push_windowing_floor.py) | 3 | Regression: a delta/resume push must window with a since-floor. |
| [test_queries.py](test_queries.py) | 30 | Tests for storage/queries.py correctness and efficiency. |
| [test_query_files.py](test_query_files.py) | 25 | Tests for user-defined SQL query files with dual variable syntax. |
| [test_reader.py](test_reader.py) | 33 | Tests for siftd.peek.reader — session file reading utilities. |
| [test_reader_edges.py](test_reader_edges.py) | 3 | — |
| [test_readonly.py](test_readonly.py) | 10 | Tests for read-only database mode. |
| [test_receive.py](test_receive.py) | 18 | Tests for siftd db receive — create-or-merge from a source database. |
| [test_resources.py](test_resources.py) | 16 | Tests for siftd.api.resources — adapter/query/formatter copy operations. |
| [test_row.py](test_row.py) | 14 | Tests for siftd.output.row — the shared row atom. |
| [test_safecall.py](test_safecall.py) | 6 | Tests for siftd.safecall — safe operations with structured error handling. |
| [test_scanner.py](test_scanner.py) | 30 | Tests for siftd.peek.scanner — session discovery utilities. |
| [test_schema_fixtures.py](test_schema_fixtures.py) | 6 | Parametrized upgrade tests: load every checked-in schema fixture, run open_database(), |
| [test_search.py](test_search.py) | 30 | Tests for search module. |
| [test_search_element_tags.py](test_search_element_tags.py) | 10 | WS2: filter-only search enumerates tagged elements; ranked hits carry tags. |
| [test_search_log.py](test_search_log.py) | 14 | Tests for search-log storage: search_events + search_opens side tables. |
| [test_search_render.py](test_search_render.py) | 12 | Tests for the painted search renderer (painted_bridge.render_search_block). |
| [test_search_rrf.py](test_search_rrf.py) | 26 | Slice-4 RRF hybrid engine: fusion, threshold rewire, fts normalization, degrade. |
| [test_search_serializer_drift.py](test_search_serializer_drift.py) | 4 | Anti-drift tests for search dataclass serialization. |
| [test_serialization_ingest_backfill.py](test_serialization_ingest_backfill.py) | 3 | Anti-drift tests for ingest/backfill result serialization. |
| [test_serialization_tags.py](test_serialization_tags.py) | 3 | — |
| [test_serve.py](test_serve.py) | 48 | Tests for siftd serve — HTTP team sync server. |
| [test_serve_app_edges.py](test_serve_app_edges.py) | 3 | — |
| [test_serve_auth_edges.py](test_serve_auth_edges.py) | 45 | — |
| [test_serve_auth_focus.py](test_serve_auth_focus.py) | 1 | — |
| [test_serve_browser_login.py](test_serve_browser_login.py) | 9 | Browser auth-code+PKCE login surface. |
| [test_serve_client.py](test_serve_client.py) | 2 | Tests for the stdlib-only siftd-serve HTTP client. |
| [test_serve_client_auth.py](test_serve_client_auth.py) | 9 | Client-side auth wiring in serve/client.py. |
| [test_serve_client_edges.py](test_serve_client_edges.py) | 16 | — |
| [test_serve_delegation.py](test_serve_delegation.py) | 19 | Tests for serve/delegation.py — generalized CLI-to-serve delegation policy. |
| [test_serve_delegation_edges.py](test_serve_delegation_edges.py) | 14 | — |
| [test_serve_delegation_wire.py](test_serve_delegation_wire.py) | 15 | Tests for wire-format expansion + parity between CLI op params and serve routes. |
| [test_serve_e2e_smoke.py](test_serve_e2e_smoke.py) | 45 | E2E smoke tests for the serve HTTP path. |
| [test_serve_fmt.py](test_serve_fmt.py) | 8 | Tests for siftd.serialization.serve_fmt — serve-side JSON renderers. |
| [test_serve_html_routes_edges.py](test_serve_html_routes_edges.py) | 8 | — |
| [test_serve_html_routes_stepup.py](test_serve_html_routes_stepup.py) | 9 | — |
| [test_serve_routes_edges.py](test_serve_routes_edges.py) | 25 | — |
| [test_serve_routes_stepup.py](test_serve_routes_stepup.py) | 8 | — |
| [test_serve_swiss_shell.py](test_serve_swiss_shell.py) | 78 | Serve-lane tests for the Swiss shell + folio/stub routes (Phase B slice 1). |
| [test_serve_tags_visible.py](test_serve_tags_visible.py) | 2 | Route test: GET /api/v1/tags?visible=activity enriches with the activity spark. |
| [test_serve_workspace_detail_route.py](test_serve_workspace_detail_route.py) | 3 | Route test for GET /api/v1/workspaces/{id} (workspace-detail Operation). |
| [test_serve_workspaces_view.py](test_serve_workspaces_view.py) | 7 | Serve-lane smoke for the Swiss Workspaces view (master ledger + detail). |
| [test_sessions.py](test_sessions.py) | 29 | Tests for live session tracking and pending tag storage. |
| [test_shell_categorization.py](test_shell_categorization.py) | 7 | Tests for categorize_shell_command — pure function with 15 categories. |
| [test_slice.py](test_slice.py) | 16 | Tests for siftd db slice — filtered database export. |
| [test_sql_helper_hygiene.py](test_sql_helper_hygiene.py) | 15 | H4: SQL helper hygiene — table and column allowlist tests. |
| [test_stats_cache.py](test_stats_cache.py) | 14 | Tests for stats cache (write at ingest, read in db stats). |
| [test_stats_usage_owner_scope.py](test_stats_usage_owner_scope.py) | 5 | Owner-scoping regression tests for the /stats usage breakdowns. |
| [test_status.py](test_status.py) | 11 | Contract tests for the status vocabulary (output/status.py). |
| [test_storage.py](test_storage.py) | 94 | Tests for siftd storage layer coverage. |
| [test_sync.py](test_sync.py) | 92 | Tests for siftd.api.sync — sync protocol utilities. |
| [test_sync_e2e.py](test_sync_e2e.py) | 9 | End-to-end sync tests: db send → db receive with real subprocesses. |
| [test_sync_http_streaming.py](test_sync_http_streaming.py) | 14 | Tests for Y3: HTTP streaming for sync push and pull. |
| [test_sync_progress.py](test_sync_progress.py) | 11 | Tests for push/pull progress emission (api/sync.py). |
| [test_sync_transport.py](test_sync_transport.py) | 14 | Tests for SSH transport in siftd.api.sync using FakeSSH. |
| [test_table.py](test_table.py) | 13 | Tests for siftd.output.table — the one width-budgeted painted table. |
| [test_tag_activity_enrichment.py](test_tag_activity_enrichment.py) | 3 | Fidelity-gated tag activity enrichment (api.tags.list_tags + 'activity' tag). |
| [test_tag_pins.py](test_tag_pins.py) | 8 | Tag pins: owner-scoped pin state, idempotent writes, the pinned flag surfaced |
| [test_tags_index_kinds.py](test_tags_index_kinds.py) | 7 | WS5: web tags index surfaces element-kind breakdown, quiet for conv-only tags. |
| [test_tags_temporal.py](test_tags_temporal.py) | 4 | Tests for list_tags temporal filtering. |
| [test_target_ref.py](test_target_ref.py) | 27 | Tests for TargetRef — the unified parse → resolve → alias tag-target layer. |
| [test_temporal_weighting.py](test_temporal_weighting.py) | 17 | Tests for temporal weighting in search.py. |
| [test_theme.py](test_theme.py) | 15 | Tests for siftd.output.theme — the bespoke "warm obsidian" palette + domain styles. |
| [test_tool_presenters.py](test_tool_presenters.py) | 39 | Tests for painted bridge tool-specific presenters and fidelity integration. |
| [test_transcript_tags.py](test_transcript_tags.py) | 3 | WS3: element tag chips in CLI conversation-detail transcripts. |
| [test_unified_search.py](test_unified_search.py) | 20 | Tests for unified search command with auto-selection. |
| [test_usage_read_sites.py](test_usage_read_sites.py) | 7 | Read-site regressions for the S2 rollup re-point (api.stats). |
| [test_usage_rollup.py](test_usage_rollup.py) | 10 | Tests for the usage_by_conv_model rollup — the keystone derived tier. |
| [test_usage_rollup_cache.py](test_usage_rollup_cache.py) | 6 | v10 cache-coherence: the rollup folds Anthropic cache tokens into the usage |
| [test_wire_form_roundtrip.py](test_wire_form_roundtrip.py) | 32 | Round-trip tests for the wire-form deserializers. |
| [test_workspace_detail.py](test_workspace_detail.py) | 7 | Behavioral tests for the workspace-detail Operation (api.stats.workspace_detail). |
| [test_workspace_detail_idor.py](test_workspace_detail_idor.py) | 4 | COMMIT-A correctness/security tests for workspace_detail. |
| [test_workspace_identity.py](test_workspace_identity.py) | 12 | Tests for workspace identity via git remote. |
| [test_workspace_pins.py](test_workspace_pins.py) | 9 | Workspace pins + list sort: owner-scoped pin state, idempotent writes, the |
| [test_workspaces_master.py](test_workspaces_master.py) | 5 | Base-lane tests for the Workspaces master list + detail cost honesty. |
| [test_workspaces_ulid_identity.py](test_workspaces_ulid_identity.py) | 2 | Workspaces are ULID-first in the read API. |

### `tests/adapters/`

| File | Tests | Summary |
|------|-------|---------|
| [adapters/test_aider.py](adapters/test_aider.py) | 7 | Tests for Aider adapter. |
| [adapters/test_aider_edges.py](adapters/test_aider_edges.py) | 1 | — |
| [adapters/test_antigravity_cli.py](adapters/test_antigravity_cli.py) | 36 | Tests for Antigravity CLI adapter. |
| [adapters/test_claude_code.py](adapters/test_claude_code.py) | 21 | Tests for Claude Code adapter. |
| [adapters/test_claude_code_edges.py](adapters/test_claude_code_edges.py) | 1 | — |
| [adapters/test_codex_cli.py](adapters/test_codex_cli.py) | 15 | Tests for Codex CLI adapter. |
| [adapters/test_copilot_cli.py](adapters/test_copilot_cli.py) | 7 | Tests for Copilot CLI adapter. |
| [adapters/test_gemini_cli.py](adapters/test_gemini_cli.py) | 11 | Tests for Gemini CLI adapter. |
| [adapters/test_golden.py](adapters/test_golden.py) | 1 | Parametrized golden-fixture tests for adapter parse() contracts. |
| [adapters/test_infra.py](adapters/test_infra.py) | 12 | Tests for adapter infrastructure: validation, registry, SDK utilities. |
| [adapters/test_jsonl_edges.py](adapters/test_jsonl_edges.py) | 3 | — |
| [adapters/test_opencode.py](adapters/test_opencode.py) | 6 | Tests for OpenCode adapter. |
| [adapters/test_opencode_edges.py](adapters/test_opencode_edges.py) | 1 | — |
| [adapters/test_pi_agent.py](adapters/test_pi_agent.py) | 4 | Tests for Pi Agent adapter. |
| [adapters/test_pi_agent_edges.py](adapters/test_pi_agent_edges.py) | 2 | — |
| [adapters/test_registry_edges.py](adapters/test_registry_edges.py) | 5 | — |
| [adapters/test_sdk_edges.py](adapters/test_sdk_edges.py) | 5 | — |
| [adapters/test_validation.py](adapters/test_validation.py) | 15 | Tests for adapter signature validation (H16). |
| [adapters/test_vscode.py](adapters/test_vscode.py) | 8 | Tests for VSCode adapter. |

### `tests/architecture/`

| File | Tests | Summary |
|------|-------|---------|
| [architecture/test_contracts.py](architecture/test_contracts.py) | 9 | CLI behavior contract tests. |
| [architecture/test_csp_fitness.py](architecture/test_csp_fitness.py) | 6 | CSP fitness functions (T1 + T2 of docs/guides/serve-browser-testing.md). |
| [architecture/test_exceptions.py](architecture/test_exceptions.py) | 3 | Every custom exception joins the taxonomy or is explicitly allowlisted. |
| [architecture/test_hard_rules.py](architecture/test_hard_rules.py) | 28 | Static code analysis tests for architectural invariants. |
| [architecture/test_imports.py](architecture/test_imports.py) | 3 | Test import dependency rules to enforce layered architecture. |

### `tests/cli/`

| File | Tests | Summary |
|------|-------|---------|
| [cli/test_cli.py](cli/test_cli.py) | 30 | CLI smoke tests — verify commands parse and run without import errors. |
| [cli/test_cli_errors.py](cli/test_cli_errors.py) | 8 | The main() taxonomy backstop: SiftdError renders clean, never a traceback. |
| [cli/test_cmd_embed.py](cli/test_cmd_embed.py) | 11 | CLI tests for 'siftd embed' — build / rebuild / status / error surfaces. |
| [cli/test_cmd_peek.py](cli/test_cmd_peek.py) | 37 | Tests for siftd peek command (cmd_peek). |
| [cli/test_cmd_search.py](cli/test_cmd_search.py) | 30 | Integration tests for 'siftd search' semantic search CLI. |
| [cli/test_data.py](cli/test_data.py) | 86 | Tests for siftd data CLI commands (ingest, backfill, migrate, doctor, copy). |
| [cli/test_db.py](cli/test_db.py) | 50 | Tests for siftd db namespace commands. |
| [cli/test_embed_status_render.py](cli/test_embed_status_render.py) | 4 | Rendering tests for 'siftd embed --status' states (base lane — synthetic status). |
| [cli/test_export_cli.py](cli/test_export_cli.py) | 15 | Tests for siftd cli export — cmd_export and build_export_parser. |
| [cli/test_filter_args.py](cli/test_filter_args.py) | 4 | Tests for the shared CLI filter argument group. |
| [cli/test_id_cmd.py](cli/test_id_cmd.py) | 17 | Tests for 'siftd id' command - ULID classification. |
| [cli/test_install.py](cli/test_install.py) | 32 | Tests for 'siftd install plugin' and 'siftd install skill' commands. |
| [cli/test_lane_grouping.py](cli/test_lane_grouping.py) | 7 | Tests for the lane-grouped top-level help (CLI UX audit, presentation slice). |
| [cli/test_meta.py](cli/test_meta.py) | 12 | Tests for siftd.cli.meta command handlers. |
| [cli/test_query_anchor_window.py](cli/test_query_anchor_window.py) | 37 | Tests for anchor + window axes on siftd show <id>. |
| [cli/test_query_noembed.py](cli/test_query_noembed.py) | 6 | Additional no-embed tests for siftd.cli.query branches. |
| [cli/test_report.py](cli/test_report.py) | 6 | Tests for `siftd report` — the canonical named-SQL runner. |
| [cli/test_search_around_argparse.py](cli/test_search_around_argparse.py) | 25 | Argparse-layer tests for siftd search --around and --turns flags. |
| [cli/test_search_history_argparse.py](cli/test_search_history_argparse.py) | 13 | Argparse-layer + behavior tests for siftd search --history. |
| [cli/test_search_noembed.py](cli/test_search_noembed.py) | 26 | Non-embeddings tests for siftd.cli.search command paths. |
| [cli/test_serve_cli.py](cli/test_serve_cli.py) | 12 | Tests for siftd cli serve — cmd_serve startup logic and build_serve_parser. |
| [cli/test_sessions_cli.py](cli/test_sessions_cli.py) | 4 | Tests for siftd.cli.sessions handlers. |
| [cli/test_show.py](cli/test_show.py) | 6 | Tests for `siftd show` — the canonical conversation reader. |
| [cli/test_show_argparse.py](cli/test_show_argparse.py) | 21 | CLI-layer argparse tests for anchor + window flags on siftd show <id>. |
| [cli/test_show_smart_routing.py](cli/test_show_smart_routing.py) | 6 | Phase 4: smart-routing of `siftd show <id>` between conversations and events. |
| [cli/test_show_tools.py](cli/test_show_tools.py) | 6 | — |
| [cli/test_tags.py](cli/test_tags.py) | 76 | Tests for siftd tag CLI command (apply, remove, list, rename, delete). |
| [cli/test_upgrade.py](cli/test_upgrade.py) | 21 | Tests for siftd upgrade command and version check. |

### `tests/snapshots/`

| File | Tests | Summary |
|------|-------|---------|
| [snapshots/test_help.py](snapshots/test_help.py) | 5 | Snapshot tests for CLI help output stability. |
<!-- gen:end -->
