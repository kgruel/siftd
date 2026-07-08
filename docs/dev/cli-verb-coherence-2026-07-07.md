# CLI verb coherence — `query`'s two residues (2026-07-07)

Design trace for 0.11.0 ("search becomes coherent"). Read-only against `main`.
The June read-surface redesign locked **search = content · query = metadata list ·
show = read one**. Two residues survive that split and the search-log feature is
about to make one of them worse. This doc traces both and recommends a sliced
change set.

---

## A. Map the metadata lane

### What `query` does today

`cmd_query` (`src/siftd/cli/query.py`) is two commands wearing one verb, forked on
whether the positional `conversation_id` is present:

| Surface | Trigger | Handler | Belongs to |
|---|---|---|---|
| **List conversations** | no positional | `list_conversations` op → `render_list` | **query** (the metadata lane) |
| Filters (`-w`, `--since/--before`, `-l` tag, `--model`, tool/tool-tag) | on list | `extract_filter_args` | **query** |
| `--stats` (view/corpus footer) | on list | `get_usage_summary` | **query** (a list-scoped summary) |
| `--oldest`, `-n/--limit`, `-v` | on list | list options | **query** |
| **Detail view** (one conversation) | positional ID | `_dispatch_detail` → `_query_detail` | **`show`** (duplicate) |
| Event detail | positional event ID | `_dispatch_detail` → `_query_event_detail` | **`show`** (duplicate) |
| Anchors `--from-start/--from-end/--at-turn/--around` | on detail | `add_anchor_window_args` | **`show`** |
| Windows `--exchanges/--turns` | on detail | window offsets | **`show`** |
| `--summary`, `--neighbors` | on detail | detail options | **`show`** |
| **`query sql <name>`** | positional == `"sql"` | `_query_sql` → deprecation notice → `report` | already dissolved to `report` (alias, prints deprecation) |

The load-bearing fact: **`show` already routes through the exact same handler.**
`cmd_show` (`src/siftd/cli/show.py`) calls `_dispatch_detail` — the code comment in
`show.py` says so outright ("`query <id>` remains a working alias that routes through
the same `_dispatch_detail` handler"). So everything in the bottom half of the table
is not *shared* logic — it is `show`'s logic, physically parked in `query.py` for test-
surface continuity, and re-exposed on the `query` parser. The list half (top of table)
is the only thing that is genuinely `query`.

**Stowaways vs native:** `--stats` is native to query (it summarizes the list/corpus,
not a single conversation). `sql` is already gone in spirit (deprecated alias to
`report`). Everything anchored/windowed/`--summary`/`--neighbors`/positional-ID is a
stowaway owned by `show`.

### What else could fall under a metadata verb

The API layer already has the full metadata-list family — they just have no first-class
CLI verb; today they surface only through `stats`, `report`, or the web UI:

| Entity | API function | CLI today | Web UI |
|---|---|---|---|
| Conversations | `list_conversations` (`api/conversations.py`) | `siftd query` | Sessions view |
| Tags | `list_tags` (`api/tags.py`) | `siftd tag list` | `/view/tags` |
| Workspaces | `list_workspaces` (`api/stats.py`) | — (only via `stats`/`report`) | `/view/workspaces` |
| Models | `list_models` (`api/stats.py`) | — | (stats) |
| Tools | `list_tools` (`api/stats.py`) | — | (stats) |
| Adapters | `list_adapters` (`api/adapters.py`) | `siftd adapters` | — |

So "what else could fall under the metadata verb if we invest in it" is concretely:
**workspaces, models, tools** — each is a `list_*` that exists in the API and is
rendered in the web UI but has no CLI home except the `report`/`stats` catch-all. This
is the real upside of treating the metadata lane as a namespace rather than a single
conversation-list verb. See slice 3.

---

## B. Weigh the names

Candidates for the conversation-list verb: keep `query`, `ls`, `list`, `sessions`.

### The collision that forces the question

The search-log feature (`docs/dev/search-log-design-2026-07-07.md`, ratified for
0.11.0) makes **"query" a first-class noun**: a `search_events` table storing the
search *query* text, an OJ about binding a later `siftd query <id>` to a preceding
search. In the same release, `query` the verb (list conversations) and `query` the
noun (the search text) will live side by side in the codebase and the docs. That is
the seam the user flagged — it is real and it is 0.11.0-concurrent, not hypothetical.

### `sessions` is already taken — and means something else

This is the decisive finding. `sessions` is **not free**:

- `src/siftd/cli/sessions.py` already owns the *session* concept: `register` /
  `session-id`, backed by the `active_sessions` table — **live harness sessions for
  deferred tagging**, not conversations. `api/peek.list_active_sessions` is the live
  scan behind it.
- The web UI's "Sessions" view (`serve/html_routes.py:719` `ui_sessions`) is a *third*
  thing again: a day-grouped **ingested** timeline of conversations, grouped by **root
  session** with sub-agents riding along (`n=50` counts top-level sessions, children
  come free — `html_routes.py:747`), over a live peek zone.

So "session" already denotes three subtly different things (live-registration row,
web timeline view, root-of-a-sub-agent-tree). Naming the CLI conversation-list verb
`sessions` would (a) collide head-on with the existing `register`/`session-id` family
that is *about* sessions, and (b) import the web UI's vocabulary into the CLI while the
domain/DB vocabulary stays `conversation` — creating exactly the **third naming seam**
the user worried about. The domain says `conversation`, the API says
`list_conversations`, the web says Sessions; making the CLI say `sessions` doesn't
resolve that tension, it hard-codes it into the most-used verb. **Reject `sessions`.**

(If anything, the web UI's "Sessions" label is itself the outlier; the CLI adopting it
would spread the outlier, not converge on it.)

### `ls` / `list`

- `list` is the honest description of the verb and reads well next to `siftd tag list`.
  But `list` as a bare top-level verb with no object is ambiguous (list *what*?) — it
  only makes sense if the metadata lane becomes a namespace (`siftd list workspaces`,
  `siftd list tags`), which is a bigger move than this release wants.
- `ls` borrows Unix muscle memory (list-the-things-here) and is short. But siftd's
  "things here" is not a directory; `ls` with `-w/-l/--since` filters reads oddly, and
  it collides with the same "list of what?" ambiguity.
- Neither collides with the search-query noun, which is their one clear advantage over
  keeping `query`.

### Keep `query`

Weight against a rename the habit cost. `query` is the single most-typed siftd verb;
it is in the muscle memory of the one user who maintains this, in the plugin command
(`plugin/commands/siftd:query.md`), the skill reference (`plugin/skills/siftd/
reference/query.md`), the `_LANES` EXPLORE lane, and dozens of docs. The
no-compat-shim precedent (`fbd05f59` removing `search --index`) was applied to a
*flag on a power path*, not to the primary daily verb — that precedent should not be
read as "rename habitual verbs freely."

### Breakage / sweep surface (the dissolution-residue rule)

A rename is not done until its trail is swept in the same change. On `main` (ignoring
the worktree copies, which carry their own trees):

- **CLI:** `build_query_parser`, `cmd_query`, the `func=cmd_query` default, `_LANES`
  EXPLORE entry, and every `siftd query` in help epilogs/examples (`query.py`,
  `show.py` cross-references).
- **Plugin:** `plugin/commands/siftd:query.md`, `plugin/skills/siftd/reference/query.md`
  (filename + body), `plugin/skills/siftd/SKILL.md`, `plugin/scripts/post-siftd.sh`.
- **Docs:** `docs/guides/install.md`, `docs/concepts/data-model.md` (×6),
  `docs/concepts/tags.md` (×12), plus `docs/ops/homelab.md` (×8) and the many
  `docs/dev/*` design/dogfood records (historical — those can stay as-is; they are
  dated records, not live reference).
- **Tests:** `tests/cli/test_query_*` (argparse, smart-routing, anchor-window),
  `tests/test_cli_query.py`, help snapshots (`test_help.ambr` ×3 Python versions).
- **Config:** `get_query_defaults()` key names (`query.limit`, `query.chars`,
  `query.tool_chars`) — a full rename of the *namespace* would touch `config.py` and
  `config.toml.example`; a verb-only rename can leave the `[query]` config section as-is.

### Alias mechanism (if we rename)

Two native levers exist, no new machinery:

1. **argparse `aliases=`** — `add_parser("list", aliases=["query"])` keeps `siftd query`
   parsing while advertising `list`. Cheapest possible deprecation shim.
2. **The `_PLUMBING` pattern** (`cli/__init__.py:92`) — register the new name in a lane,
   add the old name to `_PLUMBING` so it stays fully runnable but drops out of the
   `--help` lane view. This is exactly how `register`/`session-id` are hidden-but-live
   today. It is the softest possible break: old habit keeps working, new name is what
   gets taught.

Note the user's stated preference is explicit clean breaks over shims — but that
preference was set against a *flag*, and both levers above are effectively free to
carry. A hidden-alias-via-`_PLUMBING` is not a "shim" in the maintenance-burden sense;
it is one line in a frozenset.

---

## C. Recommended 0.11.0 change set

### Slice 1 — Detail-arg clean break (do it; cheap and certain) ✅

Remove `query`'s positional `conversation_id` and everything it drags in (anchors,
windows, `--summary`, `--neighbors`, `_dispatch_detail` branch in `cmd_query`). `show`
already owns all of it through the same handler, so **nothing moves** — the detail logic
stays in `query.py` where `show` imports it; only `query`'s *parser* stops exposing it.
`query` becomes list-only.

- Replace the positional-present branch with an exit-2 + redirect hint, mirroring the
  `search --index → siftd embed` pattern (`fbd05f59`): `siftd query <id>` →
  `error: 'query' lists conversations; to read one use 'siftd show <id>'` (exit 2).
- Fold in the already-deprecated `query sql` alias removal at the same time (it is
  separate residue but the same clean-break motion) — or leave it one more cycle; it
  already prints a deprecation notice, so it is lower priority.
- **Cost:** parser edit + delete the dispatch branch; update `test_query_smart_routing`,
  `test_query_anchor_window` (they largely move/retarget to `show`), help snapshots,
  and the query epilog examples. Contained, deterministic.
- **Confirmed cheap and certain** — no logic relocation, `show` is already the twin.

### Slice 2 — Rename: **not now.** Defer the decision to 1.0, act now only on the seam

The rename is contested (see below) and the search-query *noun* collision it is meant
to pre-empt lives in the DB/table/column layer (`search_events`, `query` column), not
in the CLI verb's user-facing surface. Renaming the verb does not de-collide the schema;
it only reduces prose ambiguity in docs. That is not worth churning the most habitual
verb + full plugin/skill/doc/test/snapshot sweep mid-0.11.0, when 0.11.0's actual theme
is search coherence.

Recommendation: **keep `query` for 0.11.0; revisit at 1.0** as part of a deliberate
verb-namespace pass (Slice 3). If we do rename at 1.0, use `list` as a *namespace*
(`siftd list [conversations|workspaces|tags|models]`, default object = conversations)
rather than a bare synonym, and carry `query` as a hidden `_PLUMBING` alias for one
release. That is the only rename that *earns* its sweep, because it also gives the
orphaned `list_workspaces`/`list_models`/`list_tools` a CLI home.

### Slice 3 — Metadata-lane additions: defer to the 1.0 namespace pass

`workspaces` / `models` / `tools` listing exists in the API and web but not the CLI.
Adding them as one-off verbs now would multiply top-level verbs; adding them as a
`list <object>` namespace is the coherent form — but that *is* the rename decision.
So bundle them with Slice 2 at 1.0, not 0.11.0. Deferring is correct: it keeps 0.11.0
scoped to search, and it means the rename (if it happens) pays for itself by unlocking
three real new surfaces instead of just relabeling one.

---

## Recommendation summary

1. **Slice 1 (detail-arg clean break): ship in 0.11.0.** Cheap, certain, no logic
   moves — `show` is already the twin handler. Exit-2 + `siftd show` redirect, matching
   the `search --index` precedent. Sweep: query parser, two test modules, help
   snapshots, epilog.
2. **Rename `query`: defer to 1.0, don't do it in 0.11.0.** The noun-collision it would
   pre-empt lives in the schema, not the verb; renaming the daily verb mid-search-release
   costs a full plugin/skill/doc/test/snapshot sweep for prose hygiene alone.
3. **`sessions` is disqualified**, not merely disfavored: the name is already taken by the
   live-session (`register`/`session-id`, `active_sessions`) family, and adopting the web
   UI's label would create a third naming seam over the domain's `conversation`.
4. **Metadata-lane growth (workspaces/models/tools) and any rename belong together** as a
   1.0 `siftd list <object>` namespace pass — that is the version of the rename that earns
   its sweep.

## Genuinely contested calls (for user ratification)

- **C1 — Rename now vs 1.0 vs never.** My rec: defer to 1.0 as a `list` *namespace*, keep
  `query` as a hidden `_PLUMBING` alias one release. The contest: the user dislikes the
  name *now* and the search-log noun lands *now* — a case exists for biting the bullet in
  0.11.0 while the search surface is already being churned (amortize the sweep). Counter:
  the collision is schema-level, not verb-level, so the rename buys prose clarity, not
  correctness.
- **C2 — Does the no-shim precedent (`fbd05f59`) apply to a verb this habitual?** My read:
  no — it was set against a power-path *flag*. If renamed, a hidden `_PLUMBING` alias is
  one frozenset line, not a maintenance-bearing shim, so it does not violate the spirit of
  the precedent. The user may hold the harder line (clean break, no alias) — that is a
  values call only they can make.
- **C3 — Fold the deprecated `query sql` removal into Slice 1, or leave it?** It already
  prints a deprecation notice and routes to `report`; removing it now is tidy but
  independent of the detail-arg break.
