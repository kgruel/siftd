# siftd / siftd-serve package split — plan

> Working doc, 2026-06-22. Local (`docs/dev/` is gitignored). Grounded against
> `main` @ `99eadefa`. Companion to `docs/concepts/architecture.md`.

## Goal

Slim the project into a lean local core (`siftd`, **with embeddings folded in** —
the 0.11.0 thread) plus a separate `siftd-serve` for the networked/web surface,
**without** losing the two things that make local serve good: the hot in-process
SQLite path and the personal HTMX UI.

## The one correction that makes this safe

Your instinct was "`siftd-serve` handles all the transport options." The code says
the right seam is **client vs. server**, not **transport vs. local** — and the
distinction is exactly what protects your concerns.

Two facts from the code:

1. **Packaging ≠ transport (the hot path is preserved).** A serve request runs
   *in-process* against local SQLite: `routes → api/dispatch.execute → op.fn(**op.to_local()) → sqlite3.connect`
   (`api/dispatch.py:91-93`). The htmx UI handlers make **zero** outbound HTTP
   calls — every `ui_*` handler calls `api/*` directly (`serve/html_routes.py`).
   So whether serve lives in one wheel or two, it imports `siftd` and hits your
   local DB directly. You only lose the hot path if you point the CLI at a
   *remote* `serve.url` — which is a config choice, not a packaging consequence.

2. **The CLIENT transports must stay in core.** If `siftd-serve` owned *all*
   transport (client too), then `siftd db push` / `db pull` / read-delegation /
   `auth login` would break for the common "laptop pushing to a homelab" user, and
   force them to install the entire server stack (litestar/uvicorn/PyJWT) they
   never run. Note the SSH sync "server" is *not* siftd-serve at all — it's the
   remote box running plain `siftd db receive` over the pipe (`api/sync.py:320`).
   So the client halves belong in core; only the HTTP *server* belongs in
   siftd-serve.

**Net: `siftd-serve` owns the HTTP *server* (REST + web UI + auth + multi-tenant).
The CLI keeps the client transports and stays a first-class remote client.**

## Post-split shape

```
┌─────────────────────────────────────────────────────────────────────────┐
│ siftd  (lean local core — single-user, local-first, a remote CLIENT)      │
│                                                                           │
│   engine:   domain · storage · ingestion · adapters(SDK+all) · api ·      │
│             search (FTS5 + embeddings CODE) · output (terminal/md/json)    │
│   client transports (stdlib): siftd/transport/{client,delegation} ·       │
│             credentials (device-code/OIDC) · cli/db send|receive|process · │
│             api/{receive,merge,slice}  (pipe primitives = local building   │
│             blocks the SSH transport drives)                              │
│   base deps: painted · tomlkit · httpx · mistune*                         │
│                                                                           │
│   ├─ [embed]  local embedding runtime — fastembed (→ onnx/numpy);          │
│   │           OR a local Ollama server (zero pip deps)                     │
│   └─ [sync]?  SSH/HTTP sync client — asyncssh (+ httpx)   ← decision       │
└───────────────────────────────┬───────────────────────────────────────────┘
                                 │  depends on  (siftd>=X)
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ siftd-serve  (separate package — the HTTP server)                         │
│                                                                           │
│   Litestar app factory · REST /api/v1 · htmx Swiss web UI (html_routes +  │
│   html_fmt + static) · auth (OIDC/PKCE/JWKS) · rate-limit · CSP ·          │
│   multi-tenant owner-scoping enforcement · push/pull receive endpoints     │
│   deps: siftd · litestar[standard] · uvicorn · PyJWT[crypto]              │
│                                                                           │
│   runs TWO ways, same code, by config:                                    │
│     • LOCAL    `siftd serve` on 127.0.0.1, auth_config=None  → personal    │
│                 dashboard, auth-free, hot path, /follow live sessions      │
│     • NETWORKED `siftd serve --host 0.0.0.0` + [serve.auth] → multi-user   │
└─────────────────────────────────────────────────────────────────────────┘
```

\* `mistune` moves to base — see decisions.

## Your two concerns, resolved

**"We lose the hot SQLite query path when serve is running."** We don't. serve is
in-process (`api/dispatch.py:91-93`); it `import siftd` and queries your local DB
directly. The split is a `pip install` boundary, not a process boundary. (The only
way to lose the hot path is to set `serve.url` to a *remote* and let the CLI
delegate — unchanged by this plan.)

**"We lose the HTMX view at the personal level."** We don't. The personal web UI
is the *same* Litestar app run locally with no auth — and that mode already works
today: `siftd serve` defaults to `127.0.0.1` (`cli/serve.py:38`), the
fail-closed-without-auth guard is **loopback-exempt** (`cli/serve.py:67-76`),
`create_app(auth_config=None)` installs no auth middleware (`serve/app.py:190-194`),
owner-scoping is inert without auth (advisory `None`), and htmx is vendored (no
CDN). So a personal user does `pip install siftd-serve && siftd serve` → browsable
dashboard, auth-free, hot path. The only change vs. today is that the UI ships in
the `siftd-serve` wheel instead of the `[serve]` extra — and `litestar`/`uvicorn`
are exactly what an HTTP UI inherently needs anyway.

> If even the second `pip install` bothers you, the fallback is to **keep serve as
> an extra** (`siftd[serve]`) rather than a separate wheel — same code seam, lower
> packaging ceremony, but litestar/auth code stays in the core repo and the
> security-audit surface isn't physically separated. The recommendation below is
> the two-wheel split; this is the escape hatch.

## Embeddings → core (the 0.11.0 thread)

Folding embeddings in is **mostly messaging, not code** — the architecture already
treats semantic as a first-class default:

- `resolve_search_mode()` (`api/search.py:869-889`) already auto-selects hybrid
  when an index + runtime exist and silently degrades to FTS otherwise; CLI and
  serve both route through it. "Semantic by default when available" is already true.
- The model (BAAI/bge-small-en-v1.5) already downloads **on first use** via
  fastembed's HF cache (`embeddings/fastembed_backend.py:25`) — decoupled from
  `pip install`.
- There's an `OllamaBackend` (`embeddings/ollama_backend.py`) that needs **zero**
  pip deps — semantic search with no heavyweight runtime if you run Ollama.

What "embeddings is core" should mean:

1. **Keep the heavy runtime opt-in.** `fastembed` pulls `onnxruntime` (native),
   `numpy`, `pillow`, … — must NOT be base. Drop the 4 redundant sibling pins from
   `[embed]` (fastembed pulls them transitively); the extra reduces to `fastembed`.
2. **One code fix** so the embeddings *code* is base-importable: remove the
   top-level `import numpy as np` from `storage/embeddings.py:13` and defer it into
   the function bodies (matching `math.py`/`search.py`). After that, the whole
   embeddings tree imports cleanly in a numpy-free base venv (verified true
   everywhere except that one line).
3. **Reframe the messaging:** "install the `[embed]` extra" → "semantic search is a
   core feature; install the local embedding runtime (`siftd install embed`) or run
   Ollama." Fix `embeddings_available()` to also recognize an Ollama backend.
4. **Decide:** does ingest auto-build the index (semantic genuinely "on" after
   ingest, but couples ingest to the model + a ~12k-chunk embedding pass), or stay
   explicit via `siftd search --index`? (See decisions.)

So `siftd` ships embeddings as a first-class capability; the *runtime* stays a thin
opt-in. Base wheel stays lean.

## Transport assignment (every path)

| Path | Client half → where | Server half → where |
|---|---|---|
| Read-delegation (CLI→remote) | `transport/{client,delegation}` → **core** (stdlib) | REST API → siftd-serve |
| htmx web UI | (browser) | **siftd-serve** (litestar) |
| REST `/api/v1` | `transport/client` → core | **siftd-serve** (litestar) |
| `db push`/`pull` (HTTP, 413-bisect) | `api/sync.py` (httpx) → core `[sync]?` | siftd-serve push/pull routes |
| `db push`/`pull` (SSH) | `api/sync.py` (asyncssh) → core `[sync]?` | remote `siftd db receive` = **core CLI** |
| send/receive pipe | `cli/db.py` + `api/receive.py` (stdlib) → **core** | (same, transport-agnostic) |
| device-code / OIDC token | `credentials.py` + `api/auth.py` (stdlib) → **core** | siftd-serve auth (JWKS) |

The pipe primitives and token-acquisition are stdlib-only and stay core regardless.
The only deps in play for the client side are `httpx` (base, keep) and `asyncssh`
(base today; the `[sync]` decision).

## The plan, phased

Ordered so each phase is independently shippable and low-risk-first.

**Phase 0 — free hygiene (no split needed).**
- Delete `math.py` (dead `cosine_similarity`, zero non-test importers) + its test.
- Move `mistune` from `[serve]` → base (it's a core CLI-markdown dep with a silent
  plaintext fallback today; `output/narrative.py:172`). Aligns with the
  terminal-UI branch, which already does this.
- Drop the 4 redundant pins from `[embed]`; lazy-import numpy in
  `storage/embeddings.py:13`.
- Extend `tests/architecture/test_imports.py` to forbid **eager imports of
  optionals** (the recurring anti-pattern). This is the guardrail the rest leans on.

**Phase 1 — embeddings to core.** The messaging/gate changes above. Mostly docs +
`availability.py` + install hints; one code fix already done in Phase 0. Decide the
auto-index question.

**Phase 2 — extract the delegation client out of `serve/` (prerequisite refactor).**
`serve/client.py` + `serve/delegation.py` are stdlib-only CLIENT code misfiled
under `serve/`. Move to `siftd/transport/` (or `siftd/remote/`) and repoint the ~10
import sites (`cli/{search,query,tags,export,meta}.py`). Pure move; this is the one
real refactor the split forces. (Optionally keep back-compat re-export shims.)

**Phase 3 — split out `siftd-serve`.**
- Monorepo, two packages, src-layout: keep `src/siftd/`; add
  `packages/siftd-serve/` with its own `pyproject.toml` (`name=siftd-serve`,
  `depends siftd>=X`, litestar/uvicorn/PyJWT).
- `git mv` `serve/{app,routes,html_routes,auth}.py` + `static/` + `output/html_fmt.py`
  into a new top-level `siftd_serve/` package (rename, **not** a `siftd.serve`
  namespace package — avoids hatchling namespace fragility).
- Registration: siftd-serve registers its app factory under a **new entry-point
  group `siftd.serve_app`** (reuse `plugin_discovery`'s existing machinery, which
  already backs `siftd.adapters`/`siftd.formatters`). `html_fmt` re-registers via
  the existing `siftd.formatters` group, so it leaves core's eager
  `format_registry.py:59` import.
- Keep the `siftd serve` **subcommand in core** as the current `require_serve()`
  stub: `siftd serve --help` still works; without siftd-serve installed it errors
  with an install hint. `cmd_serve` resolves the app factory via the entry-point
  instead of `from siftd.serve.app import create_app`.
- The ~22 `test_serve_*` files + the Chromium smoke suite move with the package;
  core's ~3,460 tests are untouched.
- Effort: ~1–1.5 days, mostly mechanical (the api/serialization boundary already
  isolates `serve → core`).

**Phase 4 — optional, later.**
- `[sync]` extra: demote `asyncssh` (and the sync client) from base to a core
  `[sync]` extra. The blocker is the eager re-export in `api/__init__.py:74,86,133`
  — make those lazy first. Trade-off in decisions.
- **De-tenant core** (the keystone, gated): `conversation_owners` is created for
  every DB (`storage/sqlite.py:243`) but written only by the server
  (`api/receive.py:138`, `api/merge.py:251`); ~12 read sites are already inert
  `has_conversation_owners_table()` guards. Once serve **and** sync are both
  optional/out, those guards + the table + the wire-duality machinery
  (`op_spec.py`, `deserialize.py`, the `to_wire`/`from_wire` half of `dispatch.py`)
  can be deleted — they *dissolve*, because "delegated render == local render" is
  vacuous with a single local source. Highest risk; do last, only if you commit to
  multi-tenancy being permanently server-side.

> Note: the split does **not** require touching `conversation_owners` — it's inert
> for single-user core. Phase 4's de-tenant is a separate cleanup, not a blocker.

## Decisions for you

1. **Two wheels or one extra?** `siftd` + `siftd-serve` (recommended: leaner core,
   physically separated audit surface, independent versioning) vs. keep
   `siftd[serve]` (less ceremony, code stays in-repo). Your call drives Phase 3.
2. **`asyncssh`/sync: base or `[sync]` extra?** Base = `db push` works out of the
   box; `[sync]` extra = leaner truly-local install but `pip install siftd[sync]`
   to push. (Recommend `[sync]` for the lean goal, but it's a default-capability call.)
3. **Auto-build the embeddings index on ingest?** On = semantic "just works" after
   ingest, at the cost of coupling ingest to the model + a multi-thousand-chunk pass.
   Off (today) = explicit `siftd search --index`. (Recommend keeping explicit; offer
   an auto-prompt.)
4. **Version pinning between wheels:** `siftd-serve` → `siftd>=X` (independent
   upgrades, wire-drift risk) vs. `==X` (lockstep). The `op_spec` wire contract is
   the compatibility surface.
5. **`mistune` home:** base dep (recommended) vs. a `siftd[html]` extra. Without it,
   core markdown output silently degrades to plaintext.
6. **Index-management CLI re-home:** `--index/--rebuild/--backend/--embed-db` hang
   off `siftd search` today. Folding embeddings into core is the natural moment to
   give index lifecycle its own verb (was deferred in the substrate redesign).

## What stays genuinely core (don't be misled)

- `domain` · `storage` · `ingestion` · adapter **SDK + all parsers** · `api` (minus
  the wire-duality machinery, which only dissolves in Phase 4) · `output`
  (terminal/md/json) · `doctor` (its `Finding` type **is** the caveats substrate —
  not separable without first hoisting `Finding` into `domain/`).
- The **client transports** (delegation, sync client, pipe primitives, token
  acquisition) — so the lean CLI stays a full remote client.
- `conversation_owners` table + its inert read-guards — harmless in single-user
  core; only siftd-serve writes it.

## Genuinely server-only (moves to siftd-serve)

`serve/{app,routes,html_routes,auth}.py` · `serve/static/` · `output/html_fmt.py` ·
`cli/serve.py`'s litestar/uvicorn usage · deps `litestar[standard]` / `uvicorn` /
`PyJWT[crypto]`. The only network-exposed, untrusted-input code in the project —
which is the real argument for the two-wheel split: it makes "siftd the local CLI"
auditable as something with no attack surface.
