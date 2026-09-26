# 0.13.0 CLI-surface design — DRAFT for ratification

**Status: DRAFT — pending Kyle's scope decisions (see §8).**

Provenance: 7-lens multi-agent audit (inventory, verb taxonomy, api↔CLI gaps, flag
conventions, output/exit-codes, agent-consumer UX, docs-drift; every proposal
adversarially verified by independent code-truth + design-coherence skeptics), merged
with a two-round external review by codex `gpt-5.6-sol` (round 1: blind advisory;
round 2: xhigh review of the merged design). All load-bearing claims in this document
were verified against source or the live binary; round-2 sol claims were independently
re-verified before inclusion.

## 1. Target surface (post-0.13.0)

```
EXPLORE   list · search · show · report · peek
CURATE    tag · export
INGEST    ingest
MAINTAIN  doctor · db · embed
SHARE     serve · auth · sync            ← sync split RATIFIED (Kyle 2026-07-18)
SETUP     install · config · copy
hidden    backfill · id · migrate · register · session-id · upgrade
          + compat aliases (ledgered, removed at 1.0): query, adapters,
            db workspaces, search --history, tag list (bare), bare report,
            export --view, copy query, db push/pull/remote (if sync ships)
```

Organizing rule (sol round 1, audit-confirmed): **`list` enumerates collections by
metadata; `search` finds content by relevance; `show` reads one identified record;
action verbs mutate — and no verb silently becomes a lister when an argument is
omitted.**

## 2. `siftd list <object>`

Real argparse subparsers (the db/auth idiom — `list <obj> --help` narrows).
Objects, each re-parenting an existing api function, zero new query logic:

| object | api function | replaces |
|---|---|---|
| conversations | `list_conversations` | `query` (verbatim flag surface) |
| workspaces | `list_workspaces` | `db workspaces` |
| models | `list_models` | — (web-only today) |
| tools | `list_tools` | — (web-only, not even in `__all__`) |
| tags | `list_tags` | bare `tag list` (drill-down forms stay in CURATE) |
| searches | `recent_search_history` | `search --history` |
| adapters | `list_adapters` | top-level `adapters` |
| reports | `list_query_files` | bare `report` |

Amendments from review:

- **Object is mandatory** (sol R2 §1): bare `siftd list` prints the object catalog
  and exits 2. No implicit conversations default — it would need duplicated args or
  argv rewriting and breaks deterministic help/completion. *(Pending decision §8 D-B.)*
- **JSON envelope** `{result, caveats, meta}` is canonical for the new namespace from
  day one (cheapest moment to establish it); compat aliases keep their old shapes
  during the window. Full ULIDs in JSON always; 12-char prefixes in human tables —
  which must actually display the ID column (today `db workspaces` terminal output
  drops it; search-history table likewise).
- **`--workspace-id`** exact selector + `list workspaces --match TEXT`: the api/storage
  plumbing already exists (`filters.py` workspace_id property, `list_conversations`
  param) — pure surface exposure, ships with the namespace.
- Plural-only objects, no singular aliases. Sorts transplant unchanged in 0.13.0
  (uniform `--sort/--order` grammar is 1.0).

## 3. Compatibility policy (new, load-bearing)

Sol R2 §2: "one-release alias" was imprecise — 0.14 (package split) lands before 1.0.
One policy, one mechanism:

- **Compat alias**: executes canonical behavior, warns once on stderr, removed at 1.0.
- **Clean break**: exits 2 with remediation hint; never called an alias.
- A single tested **deprecation ledger** (mapping + expiry) drives both; the 1.0
  removals become the ledger emptying, not a hunt.
- The 1.0 `query`→`list` redirect needs a **root-parser** unknown-command path — the
  existing `_unknown_hint` pattern is post-match/per-subcommand and never fires for an
  unregistered command (verified).

## 4. Proposal clusters (verdict-adjusted)

### A — the list namespace (defines the release)
A1 `list <object>` per §2 · A2 `query` retirement + full residue sweep (see E1; incl.
`_dispatch_detail`/detail renderers re-homed out of query.py, `render_query_detail_block`
renamed, `[query]` config section mapped, queries_dir/internal names NOT renamed) ·
A3 `adapters`→`list adapters` (INGEST becomes single-command) · A4 `db workspaces`→
`list workspaces` (also kills the render_method="raw" double-owned JSON shape) ·
A5 `search --history`→`list searches` · A6 `list tags` canonical; bare `tag list`
clean-breaks; `tag list <name>` + `--by-workspace` stay CURATE · A7 bare `report`→
`list reports`; at 1.0 `report NAME` always executes · A8 `copy query`→`copy report`
(sweep: data.py:946-947/:1595, storage.md:266; epilog documents the queries/ dir).

Derivative detection: add **token-aware `list conversations`** matching only — NOT the
substring `"siftd list"`, which would misclassify `list adapters` etc. (sol R2 §5,
contradicts test_derivative.py otherwise). `siftd ask` stays (allowlisted historical
matching over user data).

### B — grammar normalization
B1 every multi-verb command answers `<cmd> <verb> --help` scoped (tag/doctor/config are
flat parsers today — help-blind on 3 of 5); tag's `tag <id> <tag>` grammar preserved
through the window · B2 `-n` = display cap CLI-wide; tag/export selection moves to
`--last N` (long-only, value required); legacy `-n` meanings parse on compat paths
until 1.0; positive-`n` + `--all` for unbounded (kills `0 = all`) · B3 export
`--view {conversations,elements}` → boolean `--elements` (wire `view` param unchanged,
documented) · B4 export gains `-m` + `--all-tags` parity · B5 `db send` adopts full
shared filter vocab; push/pull only the subset the sync cursor carries · B6 `--no-fts`
stays registered accepted-and-ignored (permanent SSH wire contract), help=SUPPRESS,
epilog example deleted · B7 help-truth batch (search --sort citing dead `--mode`;
query/peek declared-vs-runtime default 10 — derive from the single runtime source;
doctor fix epilog; peek -w disambiguation; --tools boolean notes).

### C — machine-output contract (agents are primary consumers)
C1 `report --json` · C2 doctor --json errors emit JSON envelope; plain errors → stderr ·
C3 `db stats --json` dissolves into `json_fmt.render_stats` (65-line hand-built dict
today) · C4 targeted --json batch: `auth status`, `config get` (explicitly NOT claimed
complete — db info/path etc. remain; a read-command ratchet is the 1.0 form) ·
C5 search→show chain: verbatim text + structured highlight offsets in --json (today
`>>>…<<<` markers corrupt quoting), explicit next-step field, `show <event_id>` chain
documented in both helps · C6 (1.0) uniform `--format NAME` wherever output flows
through format_registry.

### D — safety + exit taxonomy
D1 `doctor fix` plans by default, `--apply` executes; plans are **DB-bound**
(identity + schema version + timestamp in the cache — today `doctor/fixes.py` has
none) and stale/mismatched plans refuse; auto-applicable actions separated from
advisory remediation (`copy report --force` is never automatic — it overwrites user
SQL). Evidence: the audit's own inventory agent ran `doctor fix` expecting a listing
(the epilog says "show fix commands") and mutated the prod DB. · D2 **QueryError
splits structurally** before its exit-2 contract can be honored: report-not-found /
missing-variable → UserInputError(2); file-read/operational → DriftError(1); SQL
failure classified by override-vs-shipped provenance. report.py's dead hint branch
(string-match `"Missing variables"` never matches actual messages) is replaced by
attributes on the exception, not message parsing. id_cmd's 1-vs-2 split and bare
`except Exception` fixed; tags.py's six stdout/exit-1 Usage sites become argparse-grade ·
D3 api plain-ValueError input sites fold into UserInputError; serve pane degrade-nets
gain the taxonomy class alongside ValueError (reclassification alone does not unify
serve — stated honestly) · D4 `list conversations --json --stats` puts stats in
`meta`; `show --summary --json` emits a structured summary (both are useful
combinations, not errors — the current footer-after-JSON and terminal-path-wins
behaviors are the bugs); `search --json --refs` stays an input error; peek/search
input-error exit-1 sites → 2.

### E — residue + ratchets
E1 **complete** stale-vocabulary sweep — audit scope PLUS sol's finds: peek.py:115
hint, api/slice.py:65 upgrade message, config.py:281/:534/:561, backfill.py:212,
availability.py:114, json_fmt.py:68, skill_gen (all lines), README walkthrough, the
three test assertions pinning `siftd query` in id_cmd tests. Ratchet: shipped example
lines parse through the real parser with zero unknown args · E2 schema-aware EXPLAIN
doctor check for stale report overrides (reuses drop_ins_valid structure against the
real conn; distinguishes override-shadows-builtin from builtin-needs-migration;
advisory, never auto-overwrite). **Prerequisite: the SQL-aware variable scanner** —
`_extract_query_vars`'s regex reads `:vcs`/`:test` inside `'shell:vcs'` string
literals in builtin shell-analysis.sql as required params (verified); drop_ins_valid
substitutes the same way. One scanner that skips comments/quoted literals, used by
report execution, the doctor check, and `list reports` health · E3 `copy` moves
_PLUMBING → SETUP (it is the prescribed customization path in report's epilog, README,
two concept docs) · E4 ratchet: cli/ may not import underscore-private api symbols
(shrink-only allowlist seeded with query.py's `_events_to_turn_indices`); `list_tools`
joins `__all__` when list surfaces it; NOT adopted: "__all__ must cover everything
cli/serve import" (contradicts sanctioned submodule-import convention) · E5 delegation
skeleton helper for **delegatable** list objects only (conversations/workspaces/tags
have remote ops; models/tools/searches need OpSpecs if remote parity is wanted;
adapters/reports are local catalogs) · E6 (1.0) api-owned reference resolution;
nearest-match unknown-command suggestions without plumbing leak; remediation-commands
reference generated from the fix_command registry.

## 5. Divergence adjudications (sol R2)

| # | question | ruling |
|---|---|---|
| DIV-1 | `sync` split out of `db` | **0.13.0** — this is the surface release; canonical `sync push/pull/remote/status`, `db` forms as compat aliases; `db send/receive/process/sync-status` stay hidden **permanently** (SSH wire commands — the client constructs those spellings, api/sync.py:1206); atomic or defer whole |
| DIV-2 | tag subtree | subtree + scoped help in **0.13.0**; legacy `tag ID TAG` normalized to `tag apply` during window; implicit grammar removed at **1.0** |
| DIV-3 | `list formats` | **1.0**, with C6; needs the html-formatter serve-route coupling resolved in the 0.14 split first |
| DIV-4 | `--sort KEY --order` everywhere | **1.0**; 0.13.0 transplants sorts unchanged; no `--order` where relevance has no ascending meaning |
| DIV-5 | `{result, caveats, meta}` envelope | **0.13.0 for the list namespace**; alias shapes preserved through window |
| DIV-6 | `--workspace-id` / `--match` | **0.13.0** (plumbing exists; `list workspaces` shows full IDs, so exact chaining is needed immediately) |

## 6. Release tiers

**Defining (release-gates):** A1–A8 + compat ledger · B1 · D1 · D4 · E1 · E3 ·
sync split (if D-A ratifies) · list JSON envelope · `--workspace-id` chaining ·
SQL-aware variable scanner (foundation for reports/doctor work).

**Ride-alongs (cut before weakening the above):** B2–B7 · C1–C5 (after the
QueryError/scanner foundation) · D2–D3 · E2 · E4 · E5.

**1.0:** alias/ledger removals · implicit tag grammar removal · legacy `-n` removal ·
`list formats` + C6 uniform `--format` · sort/order grammar · E6 · backfill/migrate
rehome under maintenance · typed cost/usage CLI surface (the web-only analytics
pillar — five `__all__` exports render only through the HTML dashboard today; SQL
reports are a stopgap, not a contract).

## 7. Bugs found by this audit (independent of any design decision)

1. `doctor fix` executes with no gate while its epilog says "show fix commands" (D1).
2. report.py missing-variable hint is dead code — string match never fires (D2).
3. `_extract_query_vars` regex false-positives on `:vars` inside SQL string literals;
   builtin shell-analysis.sql triggers it today (E2 prerequisite).
4. `siftd id` prints hints to the removed `query <id>`; three tests assert the broken
   string (E1).
5. skill_gen ships `siftd query <id>` ×2 + `--mode=thread` to codex/gemini/copilot/
   aider instruction files; tests monkeypatch the renderer so nothing catches it (E1).
6. `db send --no-fts` has zero effect (store_true, default=True) and the epilog
   showcases it (B6).
7. doctor --json emits bare text on error paths — `| jq` breaks (C2).
8. search --json corrupts quotable text with `>>>…<<<` markers (C5).
9. `query --json --stats` prints JSON then a terminal footer; `show --summary --json`
   ignores --json (D4).
10. Doctor fix plans apply cross-DB — cache has no DB identity/schema binding (D1).

## 8. Open decisions for ratification

- **D-A: RATIFIED (Kyle 2026-07-18) — sync splits in 0.13.0.** Rationale: the
  serve/SSH aspects are being pulled out in the package split anyway; the CLI
  namespace boundary should pre-draw the package boundary (one migration story,
  not two). Sol's atomicity constraint holds: whole split or defer whole.
- **D-B: RATIFIED (Kyle 2026-07-18) — catalog + exit 2; object is mandatory.**
  Expansion to a default remains possible at 1.0 if it grates; contraction would
  not have been. `query` compat alias covers the short spelling through the window.
- **D-C: RATIFIED (Kyle 2026-07-18) — full `tag apply|remove|list|rename|delete`
  subtree in 0.13.0; legacy forms normalize via the ledger; implicit `tag <id> <tag>`
  grammar removed at 1.0.** One parser rewrite, not two; consistent with D-B.
- **D-D:** tier trims — anything in "defining" Kyle wants out, or ride-alongs pulled up?

## 9. Artifacts

- Full audit report + 32 verified proposals + lens summaries: session scratchpad
  (`audit-report.md`, `audit-proposals.json`); sol round-1 advisory + round-2 review
  alongside. Ephemeral — the durable content is this document.
