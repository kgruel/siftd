# Dogfood Validation Report
**Branch:** dogfood-validation  
**Date:** 2026-05-13  
**Substrate:** post-read-surface-catchup (main @ 014c0dce)  
**Setup:** `./dev setup` (no `--embed` — testing embeddings-stale caveat)

---

## Invocations

**1.** `.venv/bin/siftd --help`  
Expected: top-level command listing. Got: clean output, all commands listed with short descriptions. No surprises.

**2.** `.venv/bin/siftd query --help`  
Expected: filter flags and output options. Got: well-organized into sections (filtering, tag filtering, output, fidelity, list options, detail view, sql queries). The examples section at the bottom was genuinely useful — it disambiguated `--since 7d` vs `YYYY-MM-DD` without me needing to guess. One small moment of "what does this mean?": `2p/256r` in the turns column. I had to infer this means "2 prompts / 256 responses" from context.

**3.** `.venv/bin/siftd query --since 7d -n 30`  
Expected: recent conversations to orient on activity. Got: table of 30 conversations across many workspaces. Immediately useful. Noticed many rows share the same 8-char ULID prefix (`01KRHZ9A`) — the help text at the bottom of the full `--help` explains this (8-char truncation), but I had to scan for that note. Could be confusing at first glance if you don't know ULIDs.

**4.** `.venv/bin/siftd query --since 7d -n 30 --oldest`  
Expected: same conversations, oldest-first for a timeline view. Got: it worked, showing the week from 2026-05-05 onward. Showed workspaces I hadn't seen yet in the newest-first view (`charming-kalam-77afa8`, `gallant-mirzakhani-1935dd`, `discord-scraper`, `recorded`).

**5.** `.venv/bin/siftd query --since 14d -v -n 5`  
Expected: verbose view with cost column. Got: added `Cost` and `Tags` columns. Cost shown as `?` for gpt-5.5 conversations (pricing not in the pricing db, presumably). That's informative rather than broken.

**6.** `.venv/bin/siftd query --since 7d -n 100 --json | python3 -c "..."` (awk workspace count)  
Expected: workspace frequency distribution. Got exactly that. Had to parse the JSON myself since there's no built-in `--aggregate-by workspace` view. The workspace distribution was immediately revealing:

| Workspace | Conversations (7d) |
|---|---|
| backtesting | 18 |
| meta-discussion | 16 |
| vouch | 14 |
| siftd | 12 |
| comms | 9 |
| commission | 7 |
| subtask | 6 |
| gruel-design-system | 6 |
| charming-kalam-77afa8 | 6 |

**7.** `.venv/bin/siftd query -w vouch --since 7d -n 5`  
Expected: vouch conversations. Got them. 

**8.** `.venv/bin/siftd query -w backtesting --since 7d -n 5`  
Expected: backtesting conversations. Got them. All from the same ULID prefix (`01KRGXR5`), all claude-opus-4-7, all from 2026-05-11 and 2026-05-12.

**9.** `.venv/bin/siftd query -w meta-discussion --since 7d -n 5`  
Expected: meta-discussion conversations. Got them. Mix of opus, haiku, gpt-5.5 models.

**10.** `.venv/bin/siftd query --since 7d -n 5 --json | python3` (parse for full IDs)  
Needed this to disambiguate same-prefix conversations for detail queries. The JSON structure was `{"result": [...], "caveats": [...]}` — had to discover this empirically, would have appreciated a `--help` mention of the JSON schema.

**11.** `.venv/bin/siftd query 01KRHZ9AFZHXCXNJNE1HMXASD9 --exchanges 3 -b`  
Tried to see the *first* 3 exchanges of the vouch 10-turn session. Got the **last** 3 instead — `--exchanges N` shows N most recent turns. I reached for "first N" and got "last N." Had to mentally adjust. The behavior is defensible (you usually want the end), but it surprised me.

**12.** `.venv/bin/siftd query 01KRHM5BM0BKHD225A7DN8BJDA --summary`  
Expected: metadata only for the large 931k-token meta-discussion session. Got: workspace, model, tokens, turn count. Useful sanity check before deciding to look at full content.

**13.** `.venv/bin/siftd query 01KRHM5BM0BKHD225A7DN8BJDA --exchanges 3 -b`  
Expected: early turns of the session (started 08:42). Got the last 3 turns (13:13–13:33). Confirmed the `--exchanges` direction.

**14.** `.venv/bin/siftd query 01KRHZ9A9FDKYDS5GX7DC5VFS0 -b`  
Expected: full content of the 15:19 meta-discussion session. Got a detailed view of AI agent coordination — `loops-claude`, `scrivener`, `alcove` agents communicating via `mcp__comms__wait/send`. Very revealing about the multi-agent architecture. Brief mode (80 char truncation) was appropriate here — I got the structure without being buried in full tool outputs.

**15.** `.venv/bin/siftd query 01KRGXR6BQGCT34WA9KCZ9KJ81 --exchanges 3 -b`  
Tried to see commission workspace content. Hit the tail-of-session issue again — got the `/exit` sequence, not the substance. I had to pick a different conversation ID for content.

**16.** `.venv/bin/siftd query 01KRHM5AME13DZP6QCF6SKZ4TA --exchanges 3 -b`  
Better result — got actual task content about `libs/sign-carve` (carving JWT/sign library from commission into a shared `libs/sign` package). 21/21 tests passing, committed as `0ad2732`.

**17.** `.venv/bin/siftd query 01KRGXR6AS2VWRAK1M4NYAGAHJ --exchanges 4 -b`  
Commission workspace, 279.5k token session. Got: ADR drafting for the vouch trust substrate. Three ADRs written: `0001-vouch-trust-substrate`, `0002-catalogs-as-published-trust-posture`, a third. The "gruel-network" comms system used by agents to coordinate. Very clear picture of the trust architecture in formation.

**18.** `.venv/bin/siftd query 01KRGXR6CTHMYA7VAC5ZDG5M51 --exchanges 3 -b`  
Comms workspace — commission JWT + Authentik forward-auth implementation. 795 tests passing. Phase-by-phase: JWT auth module, Authentik provision, commission JWT for MCP. Very meaty implementation session. The `-b` brief truncation made it scannable.

**19.** `.venv/bin/siftd query 01KRGXR5M4GQ6T9SAD2E6JYF0Q --exchanges 5 -b`  
Backtesting workspace — Fixed Dollar Risk (FDR) position sizing experiment. Pre-registered hypotheses in `IDEAS.md`, then ran the experiment. Analytical target ($431.80) matched observed mean ($431.27) to <0.1%. Falsifier #3: S1 is worse on P95 drawdown at every tested account size.

**20.** `.venv/bin/siftd query 01KRGXR5H0DKG97V4F2QFKPXM5 --exchanges 3 -b`  
Another backtesting session — 408k tokens, 20p/268r. "This is a real result" — performance data with symbol, n, hit rate, mean_pt, IR columns. NQ Nasdaq futures strategies with tier-based sizing.

**21.** `.venv/bin/siftd query -w commission --since 7d -n 3 --json | python3`  
Got commission conversation IDs. Confirmed the workspace structure.

**22.** `.venv/bin/siftd query -w comms --since 7d -n 3 --json | python3`  
Got comms conversation IDs. Model mix: claude-sonnet-4-6, gpt-5.5.

**23.** `.venv/bin/siftd query -w meta-discussion --since 7d -n 3 --json | python3`  
Got meta-discussion IDs.

**24.** `.venv/bin/siftd tag list`  
Expected: list of applied tags. Got: 40+ tags with conversation counts. Very useful signal for thematic priorities. Notable tags: `dissolution` (11 conversations), `forcing-function` (8), `co-creation` (5), `research:agent-team-patterns` (4), `principles:architecture` (4), `inciting-friction` (4), `observation-as-participation` (3). The `agent:sifted:finding:*` tags suggest prior dogfood runs that produced auto-tagged findings.

**25.** `.venv/bin/siftd search "vouch trust" --since 14d -n 5`  
Expected: FTS5 keyword search across content. Got: 5 results with relevance scores and highlighted terms. The note at the bottom: `Search running in keyword-only mode — install embeddings for semantic ranking: siftd install embed`. This is clearly the embeddings-not-installed signal. It appeared as a note, not a warning or caveat — I'll flag this in friction notes below. Results were relevant (ADR 0001, trust-substrate discussions, rescission lattice).

**26.** `.venv/bin/siftd search "rescission lattice" --since 14d -n 3`  
FTS5 search for specific design concept. Got 3 results, all highly relevant: the monotone lattice decision, peer thinking-time on rescission, the final "Rescission is Fact-level, not lattice-level" conclusion. The FTS5 highlighting (>>>word<<<) made it easy to scan for why each result ranked.

**27.** `.venv/bin/siftd search "backtesting NQ tier strategy" --since 14d -n 5`  
Expected: backtesting content. Got: `No results`. Four-word query with AND semantics — the backtesting conversations use "NQ" but probably not all four terms together. I should have queried simpler. This was a user error, but I didn't have enough FTS5 feedback to know why it failed.

**28.** `.venv/bin/siftd search "IdP identity provider vouch" --since 14d -n 3`  
Got results. The charming-kalam workspace had ZITADEL, Curity, Keycloak, Janssen, Authlete comparison. Commission workspace had the dissolution-test framing: "each service has its own trust layer" dissolves into "the homelab has one trust layer."

**29.** `.venv/bin/siftd query 01KRHZ9AABD1K4M4DZ4BNCH173 --exchanges 3 -b`  
idp-eval workspace — active session right now (seen in peek). Content: Kyle discussing a proposal to bring to a "sister team" for an IdP evaluation. Off-site in one week. The one-pager framing and "what can we do in a week" scope question.

**30.** `.venv/bin/siftd query 01KRGXR63105CZHRQ3QFF248A1 --exchanges 3 -b`  
gruel-design-system workspace — "Office" + "Writ" components ported from gds to commission. CSS v1.0.15→v1.0.16. Visual verification via screenshot skill. "Relay deploy" at the end. Clean shipped iteration.

**31.** `.venv/bin/siftd doctor`  
Expected: health check summary. Got:
- `[!]` (warning): 8 claude_code ingestion failures, 31 gemini_cli failures
- `[i]` (info): embeddings database exists but embedding support not installed
- `[i]`: 3 pending sessions older than 48h, 343 workspaces without git remote, files pending ingestion

The `embeddings-available` info check is the "embeddings stale" signal from the task description. It fires as `[i]` in doctor, and as a prose note in search output. Not as a formal `[!]` warning or caveat finding — whether that severity is correct is a design question (see friction notes).

**32.** `.venv/bin/siftd query --stats --since 7d`  
Expected: aggregate totals. Got: `View: 10 / 11,634 corpus | view tokens: 4646.9k / 499543.3k corpus`. The list + footer is a nice ratio display. 11,634 total conversations in the corpus.

**33.** `.venv/bin/siftd peek`  
Expected: live sessions. Got a real-time snapshot — 10 active sessions visible right now:
- This dogfood session (`siftd [dogfood-validation]`)
- `loops` agent (2 sessions, claude-opus-4-7)
- `idp-eval` (10 exchanges, ongoing)
- `vouch [e2e/reference-verifier]` (gpt-5.5 + claude-opus-4-7 in parallel)
- `vouch` (14 exchanges)
- `subtask` (32 exchanges)
- `siftd` (3 and 48 exchanges)

The peek output was the most immediately grounding moment of the whole session — seeing the agent network live made it concrete.

---

## Answer: What Has Kyle Been Thinking About Lately?

### The dominant thread: vouch trust substrate

The single heaviest area of recent work, spanning `vouch`, `commission`, `comms`, and `meta-discussion`, is a **cryptographic trust substrate for a homelab multi-agent system**.

The core concept: agents ("scrivener", "loops-claude", "alcove", "cairn", "carom") can cryptographically vouch for each other via the `vouch` system. The trust lattice is **monotone** — delegation can only narrow scope, never expand it. Rescission operates at the **Fact level**, not the lattice level: instead of mutating the lattice, rescission events are recorded as facts, and "active scope at time t" is a fold over `(issue, rescind)` fact sequences.

This week's work included:
- Three architectural decision records drafted overnight by `claude-commission` (ADR 0001: vouch-trust-substrate, ADR 0002: catalogs-as-published-trust-posture)
- `commission` project: JWT + JWKS signing infrastructure, Authentik forward-auth, MCP authentication (795 tests)
- `libs/sign` carved from commission into a shared library (`libs/sign-carve` subtask)
- `vouch` project: rescission write-path shipped, merged as `49565b6`
- Active E2E `reference-verifier` branch running right now (visible in `peek`)

The naming is deliberate and consistent: vouch, writ, warrant, commission, rescission, lattice — a vocabulary drawn from legal/medieval documents to describe cryptographic delegation.

### The complementary thread: IdP evaluation

A quieter but active thread in `idp-eval`: Kyle is preparing a proposal for a **sister team's identity provider evaluation**. He's converged on framing it as a "progressive enhancement" one-pager for a team off-site in one week. The session from 17:24 discussed ZITADEL, Curity, Keycloak, Janssen, Authlete as comparators. The trust substrate Kyle is building in vouch/commission is designed to dissolve the need for per-service trust layers into one homelab trust layer — the IdP evaluation is the external lens on this.

### Separate domain: algorithmic trading backtesting

In parallel, Kyle has been running systematic trading strategy research on NQ (Nasdaq) futures. The methodology is rigorous:
- Pre-register hypotheses in `IDEAS.md` before running experiments
- Falsification criteria stated upfront
- Metrics: hit rate, mean P&L, information ratio (IR), P95 drawdown
- Current focus: Fixed Dollar Risk (FDR) position sizing for "tier 1" NQ strategies

This week's conclusion: S1 FDR sizing is worse than baseline on P95 drawdown at every tested account size. The 2025-2026 data dependence is real. Kyle planned to pick up "tier 1" in the next session for live/parameter play.

### Infrastructure: the AI coordination layer

The `comms` project (9 conversations, 7 days) and `loops` workspace (visible in `peek`, 2 live sessions right now) reveal that Kyle is running a **multi-agent coordination network**. AI agents communicate via `mcp__comms__wait/send` in named channels (`#vouch-impl`, `#trust-substrate`). The `loops-claude` agent acts as a long-running coordinator, polling for messages and emitting architectural decisions to a "project store." This is the infrastructure that makes the parallel subtask work coherent.

### Design system iteration

Six conversations in `gruel-design-system` this week. The current iteration: porting "Office" and "Writ" components from the design system into commission's web interface, with palette-aware dark mode. Published to CDN, deployed via Relay.

### Siftd itself (this project)

12 conversations this week. The read-surface catchup completed today: Fidelity contract threaded into the fetch layer, search flags dissolved from 4 booleans into 3 orthogonal axes, role labels changed from storage-kind to role-first. Now in dogfood validation (this session), then skill rewrite.

---

## Friction Notes

**1. `--exchanges N` is tail, not head.**  
I reached for it expecting the first N turns. Every time, I got the last N. For understanding what a session was *about*, you almost always want the beginning, not the end. There's no `--first N` or `--from-start` flag. I had to look for sessions that happened to have substantive content near the end, or use full views. The help text doesn't clarify the direction.

**2. Same 8-char ULID prefix for many conversations.**  
Multiple conversations share the same 8-character prefix when they started near the same time. The help text explains this, but you have to scroll to find it. Using a truncated ID in `query <id>` with a collision is an ambiguous state — I wasn't sure if it matched the first, the last, or errored. I avoided ambiguous prefixes by using `--json` to get full IDs, but that's a workaround.

**3. Search failure gives no diagnostic.**  
`.venv/bin/siftd search "backtesting NQ tier strategy"` returned `No results`. Four FTS5 terms are ANDed — any missing term zeros the results. No hint like "try fewer terms" or "found 2/4 terms in X documents." I didn't know if it was a corpus miss or a query structure issue.

**4. The embeddings-not-installed signal has two forms.**  
In `doctor`, it's `[i] embeddings-available: Embeddings database exists but embedding support not installed`. In search output, it's a prose note at the bottom: `Search running in keyword-only mode — install embeddings for semantic ranking: siftd install embed`. These are the same finding with different severity labels and different paths. The doctor `[i]` (info) feels low for a capability that's technically installed (the DB exists) but not usable. A `[!]` might be more appropriate. The search note is easy to miss scrolled past results.

**5. No way to see conversation context / threading across a week.**  
I had to manually correlate workspaces → projects → themes. The tag system helps (tagged conversations surfaced with `tag list`), but most recent conversations are untagged. A "what were the themes this week?" query would require embeddings + clustering. With keyword-only FTS5, you have to already know what to search for. The workspace distribution query I ran (command 6) had to be done with shell pipelines + Python, not a built-in.

**6. `siftd query -w <workspace>` with very common workspace names.**  
`-w vouch` matches workspace path substring — `/Users/kaygee/Code/vouch`. This worked cleanly. But I was briefly unsure if `-w vouch` would also match a workspace called `/Users/kaygee/Code/vouch-something`. It didn't come up, but the substring semantics are silently implicit.

**7. `--stats` output mixed with the regular list.**  
The stats footer (`View: 10 / 11,634 corpus`) appears after the list table. With `-n 30`, you scroll past 30 rows to reach it. For workspace-aggregation use, I would have wanted the stats at the top or available standalone.

**8. `--exchanges` + `--oldest` don't compose for "first N turns".**  
`--oldest` sorts the list view. It has no effect inside `query <id>` detail view. There's no way to say "show me the first 3 exchanges of this conversation." This was the sharpest navigation gap.

---

## Findings That Pointed Somewhere Useful

- `siftd peek` was the single highest signal-to-noise command I ran. Seeing 10 live sessions — including the `loops` agent and the `vouch [e2e/reference-verifier]` branch actively running — grounded everything I'd read in conversations in a real-time picture.
- The `tag list` output surfaced the thematic vocabulary (`dissolution`, `forcing-function`, `inciting-friction`, `observation-as-participation`) which I wouldn't have guessed from workspace names alone.
- FTS5 search on `rescission lattice` surfaced a very specific architectural decision with high precision. The `>>>term<<<` highlighting made results scannable.
- `--json` output was essential for disambiguating same-prefix ULIDs — this workflow (list → json → extract full ID → detail query) could be smoother.
