<!--
  Canonical README content. Structure = the agreed spine; Claude Design pours chrome
  (the .dc.html system) over this. Section order and copy are the contract; the dark
  terminal cards in the mockup correspond to the fenced ``` blocks here.

  DESIGN PUNCH-LIST vs the first-pass mockup (siftd-readme.dc.html):
    · Hook → fused version below (was human-first "made searchable")
    · ADD section 05 "Use with agents" — absent in the mockup; it's the differentiator
    · PROMOTE the web UI to its own section 06 with a REAL screenshot (folio + reckoning).
      The mockup only shows the `serve` startup banner as a terminal card — the actual
      views never appear. A terminal card can't carry the folio/dashboard; this one needs a render.
    · SPLIT the mockup's overloaded §07 into three stops: 06 browser · 07 machines · 08 team
    · Hero install: `uv tool install` (not `pip install`)
    · Adapters: 8, not 4
    · Commands table: drop the removed `tools`; add `show`/`report`; the `search` row must
      NOT say "requires [embed]" (FTS works without it); group by the CLI's own lanes
    · Verify "browser SSO": local default is `--no-auth`, not SSO
-->

<!-- HERO (Design): wordmark + side-by-side of painted terminal (a search) and the web folio. -->

# siftd

Every session with Claude Code, Codex, Aider, or Gemini leaves a trail — decisions made, problems solved, dead ends explored — in a log file you'll never open again.

siftd turns that trail into a searchable memory: **query it from your terminal, browse it in your browser, and let your agents recall it mid-task.**

```bash
uv tool install siftd     # or: pipx install siftd · pip install siftd
```

<!-- BADGES (Design): PyPI · Python 3.12+ · MIT · CI -->

*Runs locally · no API calls · your logs never leave your machine.*

---

## 01 · You have sessions everywhere
<sub>`ingest · adapters`</sub>

Run your first ingest. siftd reads the logs your tools already write — nothing to configure.

```
$ siftd ingest
==================================================
SUMMARY
==================================================
Files found:    523
Files ingested: 448
Files skipped:  75

Conversations: 448   Prompts: 6,241   Responses: 7,893   Tool calls: 52,107

--- By harness ---
claude_code   312 conversations · 4,102 prompts · 41,893 tool calls
codex_cli      89 conversations · 1,456 prompts ·  7,241 tool calls
gemini_cli     47 conversations ·   683 prompts ·  2,973 tool calls
```

That's a few months of work — prompts, responses, tool calls, file edits, shell commands — now structured and queryable. siftd ships adapters for **eight** tools out of the box (see [Adapters](#adapters)).

---

## 02 · You remember working on something
<sub>`keyword → semantic`</sub>

A week ago you solved a tricky auth problem. You don't remember which project or what you called it — only the shape of the problem.

```
$ siftd search "token refresh"
01JGK3M2P4Q5  2026-06-15  payments-api    12p/34r
01JFXN2R1K4M  2026-05-03  auth-service     8p/19r
```

Keyword search (FTS5) works the moment you install — no setup. But it only matches words you actually typed; "session expiry" or "credential renewal" won't surface. Add the `[embed]` extra and the *same command* searches by meaning. Embeddings build locally, no API calls.

```
$ siftd install embed
$ siftd search --index                      # build the index, one time
$ siftd search "handling expired credentials"

  01JGK3M2P4Q5  0.847  RESPONSE  payments-api
    The refresh uses a sliding window — store the refresh token in an
    httpOnly cookie, check expiry on each request rather than waiting…
  01JFXN2R1K4M  0.812  RESPONSE  auth-service
    For credential renewal we went with a background refresh 30s before
    expiry instead of reacting to a 401…
```

A different project, different words — found because the meaning matched. Narrow it, or pull in the surrounding turns:

```bash
siftd search -w payments-api "retry"               # one workspace
siftd search --since 7d "flaky test"               # recent only
siftd search "why retry" --around retry --turns -2:+2   # window around the phrase
siftd search "design decision" --view thread       # expand top hits into full threads
```

> The engine (`--mode`) and the result shape (`--view`) are independent — full surface in [search](docs/concepts/search.md).

---

## 03 · This is useful — you'll need it again
<sub>`tag · export`</sub>

You found the conversation. Mark it so you can find it instantly next time — tags are freeform; prefixes make namespaces.

```bash
siftd tag 01JGK3 decision:auth
siftd tag -n 1 research:oauth        # newest conversation, no ID needed
siftd query -l decision:auth         # retrieve one tag
siftd query -l decision:             # or a whole namespace
siftd tag list                       # browse what you've marked
```

When you're opening a PR — or feeding context back into an agent — pull a conversation out as clean markdown:

```bash
siftd export 01JGK3 -o context.md
siftd export --last 3                 # the last three sessions
siftd export -w payments-api --since 7d
```

> [Tags](docs/concepts/tags.md) covers naming conventions and auto-applied tags.

---

## 04 · You want to see a session in progress
<sub>`reads logs directly`</sub>

Ingest runs periodically, but sometimes you want what's happening *right now*. `peek` reads the log files directly — useful for checking on a long-running agent before it's ingested.

```
$ siftd peek
c520f862  payments-api   just now   12 exchanges   claude_code
a3d91bc7  auth-service   2h ago      8 exchanges   claude_code

$ siftd peek c520 --follow            # tail it live, like tail -f
```

---

## 05 · Built to be used by your agents
<sub>`record + recall`</sub>

siftd isn't only something *you* search — it's a memory your coding agents write to and read from. It works in both directions.

**They record into it automatically.** `siftd install plugin` wires lifecycle hooks into Claude Code: each session registers when it starts and re-ingests as you work, with sub-agent runs nested under their parent. No manual `ingest` for your Claude Code sessions.

**They recall from it on demand.** `siftd install skill` puts the *real* search engine in front of Claude Code, Pi, Codex, Gemini, Aider, and Copilot — a `/siftd` skill in Claude Code and Pi, a plain instructions file in the rest. However it's surfaced, the agent runs the actual CLI:

```
/siftd "how did we handle rate limits"   → searches past sessions for the answer
/siftd --recent                          → the last few conversations
/siftd --genesis "auth design"           → the first time a concept appears
/siftd:tag decision:caching              → bookmark the current session mid-task
```

*Shown as Claude Code commands; elsewhere the agent issues the same `siftd` queries itself. `/siftd:tag` ships with `siftd install plugin`.*

Your agent stops re-deriving what you already worked out last month and reads the decision instead.

> See [Using siftd with agents](docs/guides/agents.md) — the hooks, the full skill vocabulary, and tagging conventions for agent-built memory.

---

## 06 · You'd rather read it in a browser
<sub>`siftd serve` · local web UI`</sub>

For reading rather than grepping, siftd ships a local web UI — the same corpus, point-and-click.

```bash
siftd install serve
siftd serve --no-auth        # → http://127.0.0.1:8484
```

<!-- SCREENSHOT (Design): the folio reading view (with cost/tags ledger sidebar) and the reckoning dashboard. Light tone. This is the marquee visual — it shows what 0.10.0 actually shipped. -->

- **Folio** — a conversation as an editorial transcript (prose *reading* view) that toggles to a *trace* of the raw event sequence, tool I/O and all.
- **Sessions** — live and ingested sessions in one timeline, sub-agents nested under their parent.
- **Reckoning** — a token-and-cost dashboard: trends by hour and day, ranked by model and workspace, brushable.
- **Find** — the same search engine as the CLI, driven by facet filters, with the whole query carried in a shareable URL.

Every view is URL-addressable (`/?view=…`), so any state — a search, a filtered dashboard, a conversation open to one event — is a link you can bookmark or send.

> Full tour of the views and the URL grammar: [the web UI](docs/concepts/web-ui.md).

---

## 07 · You work across machines
<sub>`db remote · push · pull`</sub>

You ingest on your laptop during the day and a home server at night. Sync keeps both complete — it works like git remotes:

```bash
siftd db remote add alcove deploy@192.168.1.44:/data/siftd/team.db
siftd db push alcove          # send your deltas
siftd db pull alcove          # bring back theirs
```

Remotes can be SSH hosts or local paths (a NAS, an external drive). Transfers are delta-only since the last sync, merge cleanly by ID, and deduplicate content by hash. The same `-w` / `--since` / `-l` filters scope what you share.

> [Sync](docs/concepts/sync.md) covers transports, delta tracking, and the `send`/`receive` pipe primitives.

---

## 08 · Your team wants one picture
<sub>`serve + auth`</sub>

`siftd serve` is also a real server. Point it at a shared database and it adds what a team needs: bearer-token auth (OIDC or RFC 7662 introspection — it validates tokens, it doesn't issue them), push **attribution** (who pushed what, when), and **remote query** so clients search the server without pulling the whole database down.

```bash
pip install 'siftd[serve]'
siftd --db /data/team.db serve        # run behind a reverse proxy for TLS

# on each teammate's machine:
siftd db remote add team https://siftd.example.com
siftd auth login                      # OAuth device-code; tokens refresh automatically
siftd search "rate limiting"          # delegates to the team corpus
```

> [Serve](docs/concepts/serve.md) covers auth modes, deployment (Docker/systemd), and the HTTP API.

---

## Adapters

siftd ships adapters for eight tools out of the box:

| | | | |
|---|---|---|---|
| Claude Code | Codex CLI | Pi Agent | OpenCode |
| Aider | Gemini CLI | Copilot CLI | VSCode / Cursor / Windsurf |

Use something else? Drop in your own — no fork required:

```bash
siftd copy adapter template     # scaffold into ~/.config/siftd/adapters/
siftd ingest -v                 # verify it parses
```

An adapter is one module: declare where the logs live, return a `Conversation` from a file. See [Writing adapters](docs/guides/writing-adapters.md).

---

## Commands

```
EXPLORE   query · search · show · report · peek
CURATE    tag · export
INGEST    ingest · adapters
MAINTAIN  doctor · db
SHARE     serve · auth
SETUP     install · config
```

Run `siftd <command> --help` for full options, or browse the [CLI reference](docs/reference/cli.md).

## Documentation

[Concepts, guides, and reference →](docs/index.md)

## License

MIT · *the gold is the point*
