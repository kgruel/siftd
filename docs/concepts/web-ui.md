# The web UI

`siftd serve` ships a browser front-end over the same database the CLI reads. The CLI is built for one question at a time — search, open, tag, move on. The web UI is built for *reading*: following a conversation as prose, tracing what an agent actually did, watching token and cost trends, and browsing the corpus by tag or workspace. Every view is a URL, so what you're looking at is always something you can bookmark or paste to a teammate.

This page is about what you see and do in the browser. For how to *run* and *secure* the server — auth, deployment, TLS, attribution — see [Serve](serve.md).

## Launch it locally

The server lives behind the `[serve]` extra. Install it, then start the server with auth off for local use:

```bash
siftd install serve
siftd serve --no-auth
```

```
siftd serve — listening on 127.0.0.1:8484
  url   http://127.0.0.1:8484/
  db    /home/you/.local/share/siftd/siftd.db
  auth  disabled (--no-auth)
```

Open <http://127.0.0.1:8484> and you land on the latest conversation.

A few defaults worth knowing:

- The server binds `127.0.0.1:8484` by default — loopback only, reachable just from your machine. Override with `--host` / `--port`.
- `--no-auth` is **development only**. The server fails closed on a public bind: if `--host` is anything but loopback and auth is off, it refuses to start unless you also pass `--unsafe-public-no-auth`. That combination exposes the entire corpus for read *and* write, so it's never the accidental default.
- If the database doesn't exist yet, it's created with an empty schema before serving — you can launch on a fresh machine and ingest later.

```bash
siftd serve --host 0.0.0.0 --port 9000    # refused without [serve.auth] or --unsafe-public-no-auth
```

## The shell

The whole UI is one page: a fixed left rail of six views and a surface that swaps content into place. There's no full reload as you move around — the rail mounts each view into the surface — but the address bar always reflects where you are (see [URL-as-state](#url-as-state) below).

The rail foot carries a light/dark **tone toggle**. Your choice is written to `localStorage` (`siftd-tone`), so it persists across visits and reloads; the default is light.

The six views, in rail order:

| # | View | Route | What it is |
|---|------|-------|------------|
| 01 | Sessions | `/view/sessions` | Live + ingested timeline |
| 02 | Search | `/find` | Faceted content + metadata search |
| 03 | Transcript | `/folio` | Prose reading view of one conversation |
| 04 | Tags | `/view/tags` | Pinned tags + namespace tree |
| 05 | Workspaces | `/view/workspaces` | Workspace explorer |
| 06 | Stats | `/dashboard` | Token & cost analytics |

Those routes are internal mount targets. Hitting one directly in the browser (a typed URL, a refresh, a shared link) 303-redirects to the canonical shell URL, so you always land on the full chrome rather than a bare fragment.

## Transcript — the folio

```
/folio?id=<conv>&mode=reading|trace&event=<ulid>
```

The folio is the reading view: one conversation rendered as prose, with tool calls and cost folded into a ledger sidebar. With no `?id=` it opens the most recent conversation, so the view is never empty.

The `mode` toggle switches between two shapes of the same conversation:

- **reading** (default) — prose. The model's text, your prompts, tools summarized in the ledger.
- **trace** — the raw event sequence. Tool inputs and results are inlined in the order they actually happened, so you can follow what the agent did step by step.

`?event=<ulid>` deep-links to a single event inside the trace — this is how a search hit jumps you straight to the matched exchange rather than the top of the conversation. The ledger sidebar shows the cost and the tags ledger; tags are editable in place.

```
/?view=transcript&id=01JABC...                 # reading
/?view=transcript&id=01JABC...&mode=trace       # the event trace
```

## Sessions

```
/view/sessions          # live + ingested
/follow?sid=<id>         # tail one live session
```

Sessions is a timeline. The top zone is **live** — sessions discovered by scanning the server host's session files in real time, the same scan `siftd peek` does. Below it is the **ingested** corpus, grouped by day, with sub-agent conversations nested under their parent.

Click a live session and `/follow?sid=<id>` tails it: the folio renders from the live source and self-refreshes, pinned to the bottom like `tail -f`.

Live data comes off the server host's filesystem and bypasses the database's per-owner scoping, so on a public/shared deployment it's turned off (`serve.allow_live_endpoints`); the Sessions view then shows the ingested zone only and `/follow` isn't registered.

## Reckoning — the Stats dashboard

```
/dashboard?model=<name>
```

Stats answers "where did the tokens and dollars go." It plots usage over time, then breaks the corpus down two ways:

- **Trends** — a daily activity timeline over the period, plus hour-of-day and day-of-week rhythms.
- **Rankings** — by model and by workspace.

A **Tokens | Cost** measure toggle re-scales every chart and re-sorts the rankings. Click a row in the model ranking to **brush** the charts — the trend plots and the input-economy strip re-scope to just that model. The brushed model rides the URL as `?model=`, so a brushed dashboard is a shareable link; an unknown model falls back to the unscoped view.

```
/?view=stats&model=claude-sonnet-4
```

## Tags

```
/view/tags
```

Tags shows your pinned tags up top, then the full tag set as a namespace tree — flat tag names are split on `:` (so `review:ship` and `review:flaky` nest under `review`). Pin or unpin a tag in place; drill into any tag to open Find pre-filtered by it (`/find?tag=<name>`).

## Workspaces

```
/view/workspaces?sort=sessions|recent|tokens|cost
/workspace?ws=<id>
```

Workspaces is a two-tier explorer: pinned and recent workspaces as cards at the head, then the full list below. Each row carries its session count, tokens, and cost. The `?sort=` knob reorders the list along any of those axes (default `sessions`) and the magnitude bars follow. Drill into a row to open that workspace's detail at `/workspace?ws=<id>`.

```
/?view=workspaces&sort=tokens
```

## Find — search

```
/find?q=&engine=auto|hybrid|semantic|fts
      &shape=chunks|thread|conversations
      &workspace=&model=&tag=&tool=&owner=&since=&before=
      &sort=score|time&threshold=&full=
```

Find is the **same search engine the CLI runs** — `siftd search` and the REST `/api/v1/search` route both call into it — driven by facets instead of flags. Type a query and the results route through ranked search; leave the box empty and an active facet, and you browse that slice in recency order.

The control strip maps one-to-one onto the URL:

| Facet | Param | Notes |
|-------|-------|-------|
| Query | `q` | the content search term |
| Engine | `engine` | `auto` (default), `hybrid`, `semantic`, `fts` — the engine toggle only appears when this server has embeddings; without them everything is keyword search |
| Result shape | `shape` | `chunks` excerpts (default), `thread`, `conversations` — the CLI's `--view` shapes |
| Workspace / Model / Tag / Tool | `workspace` `model` `tag` `tool` | filter which conversations the query draws from |
| Owner | `owner` | filter by identity |
| Date window | `since` / `before` | |
| Order | `sort` | `score` (default) or `time` |
| Threshold | `threshold` | minimum relevance score, post-filter |
| Full text | `full` | untruncated excerpts (the CLI's `--verbose`) |

Because the whole query lives in the URL, a search is shareable and reproducible — paste the link and your colleague sees the same hits, facets, engine, and ordering.

```
/?view=search&q=flaky+test&engine=semantic&shape=thread&workspace=siftd
```

If you ask for `semantic` or `hybrid` on a server that has no embeddings, Find degrades to keyword search rather than erroring, and the result header tells you which engine actually ran.

## URL-as-state

The address bar is always a canonical shell URL:

```
/?view=<id>&<state>
```

`view` is one of `transcript`, `search`, `sessions`, `tags`, `workspaces`, `stats`. Each view's state rides as query params — and **only non-default values appear**. A score-sorted, chunks-shaped, auto-engine search collapses to a clean `/?view=search&q=…`; flip a knob off its default and it joins the URL. This keeps shared links minimal and readable.

| View | State params |
|------|--------------|
| `transcript` | `id`, `mode` (when `trace`), `event` |
| `search` | `q`, `shape`, `engine`, `workspace`, `model`, `tag`, `tool`, `owner`, `since`, `before`, `sort`, `threshold`, `full` |
| `sessions` | `follow` |
| `tags` | — |
| `workspaces` | `ws`, `sort` |
| `stats` | `model` |

Back and forward work the way you'd expect: each view push is a history entry, so navigating away from a folio and back restores the search you came from. A handful of legacy presence-based deep links also resolve — `/?id=` opens the transcript, `/?q=` or `/?tag=` opens search, `/?ws=` opens a workspace, `/?follow=` opens a session tail — so older bookmarks keep working.

## See also

- [Serve](serve.md) — running and securing the server: auth, deployment, attribution, the JSON/sync API.
- [Search](search.md) — the engine behind Find: FTS5, embeddings, hybrid ranking.
- [Tags](tags.md) — naming conventions behind the namespace tree.
