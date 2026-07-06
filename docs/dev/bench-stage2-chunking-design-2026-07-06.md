# Bench stage-2 — chunking redesign experiment (design, 2026-07-06)

Status: **design only.** Promote rules are NOT pre-committed here — stage 1's lesson is
that gates get ratified with the user *before* data. This doc lays out the experiment
structure, the hypotheses, and the open decisions for the fresh session. Companion to
`bench-plan-2026-07-05.md` (stage 1) and `bench-stage1-results-2026-07-06.md`.

## Why this exists (what stage 1 surfaced)

Stage 1 compared narrow-then-rank vs RRF fusion over a **fixed** chunking. Two findings
made chunking the next variable:

1. **The RRF verdict was a conversation-dedup artifact.** narrow-then-rank's output is
   MMR conversation-deduped; the RRF path's fused output is not. Scored at 10 chunk
   *slots*, a conversation with many matching chunks floods the budget and the metric
   counts the waste as misses. Re-scored at 10 distinct *conversations* (`sweep.py
   --unit conversation`), RRF wins composite on voyage (+0.013) and gemini (+0.015),
   passes all no-worse gates on voyage with paraphrase +10.3%, and loses only on the
   weak bge arm. **Flooding is a chunk-granularity phenomenon** — one conversation is
   many chunks competing as independent units for a fixed slot budget.

2. **The current chunker discards structure we already have.** `chunker.py` produces:
   - `exchange` chunks (95%): each = one user prompt **concatenated with** the
     assistant response, then accumulated into ~256-token windows (max 512, 25 overlap).
   - `tool_summary` chunks (5%): one synthetic per-conversation summary of all tools /
     files / commands.

   So prompt and response are **always blended** into one embedding; **thinking blocks
   and tool inputs/outputs are not embedded at all** (only the coarse summary). The
   event layer distinguishes prompt / response / thinking / tool_call kinds and the
   `chunk_type` column already exists — the schema is ready for typed chunks; the
   chunker collapses them. Distribution: avg 13.4 chunks/conv, **max 517**, 942 convs
   with 40+ (the flooders).

The question underneath both: **is the retrieval unit the chunk or the conversation?**
Every metric scores at conversation granularity; the index, fusion, and flooding all
operate at chunk granularity. Stage 2 settles that seam before any RRF-vs-narrow
ratification, because a better chunking + a rollup stage could change the answer again
(e.g. make RRF robust even on weak models by fixing flooding at its source).

## The two levers

### Lever A — type-aware chunking

Replace the blended exchange chunk with chunks carried on distinct `chunk_type`, each
still stamping `source_ids` (its event ids) for the FTS→chunk bridge:

- `prompt` — user prompt text (windowed if long)
- `response` — assistant response text (windowed)
- `thinking` — reasoning blocks (windowed, capped — these can be large) — **new surface**
- `tool_call` — tool invocation + input, per call or grouped — replaces the coarse
  per-conversation summary with retrievable per-action chunks

Candidate strategy variants (bounded to control re-embed cost):

| id | strategy | tests |
|---|---|---|
| S0 | current exchange-window (baseline) | the incumbent |
| S1 | prompt / response split | does un-blending help paraphrase (query matches prompt without response dilution)? |
| S2 | S1 + thinking chunks | does indexing reasoning improve recall? |
| S3 | S1 + thinking + per-call tool chunks | full type-awareness vs the coarse summary |

Granularity is a secondary axis (target_tokens ∈ {128, 256, 512}) — sweep only on the
winning typing to keep cost bounded.

### Lever B — conversation rollup (a real engine stage, not a metric hack)

Add a rollup stage in `search.py` that collapses chunk-level results to conversations
before returning top-n — applied **uniformly** to narrow AND rrf, so the comparison is
symmetric by construction (not fixed up in the metric as stage 1's `--unit
conversation` did offline). Two designs to compare:

- **dedup**: keep the best-ranked chunk per conversation (what the fair metric
  simulated; needs no embeddings, so it dodges the entrant-KeyError that blocked
  post-fusion MMR).
- **aggregate**: combine a conversation's chunk scores (max or sum) so a conversation
  with several relevant chunks outranks one with a single lucky chunk.

This makes conversation the retrieval unit while chunks stay the matching unit —
resolving the granularity/scoring seam directly.

## Hypotheses (to ratify with the user, not pre-committed here)

- **H-A1**: prompt/response split raises paraphrase recall on the strong arms (query
  aligns to prompt-type chunks without assistant-answer dilution).
- **H-A2**: indexing thinking + tool content raises recall — likely needs a new
  ground-truth class ("what was I reasoning about when…", "the action that touched X")
  since the current 5 classes don't target those surfaces.
- **H-B1**: a rollup engine stage reproduces the fair-metric gains *in the shipped
  product*, and — by fixing flooding at the source — narrows or closes RRF's bge gap,
  possibly making RRF a model-blind default after all.
- **H-B2**: rollup=aggregate beats rollup=dedup (multi-chunk evidence should count).
- **H-C**: there is a granularity sweet spot (finer = sharper chunks but more flooding
  pressure; the rollup should make the system granularity-robust).

## What to build (all reuses stage-1 infra)

Reused as-is: the 632-query ground truth, the three arms' snapshot + corpus, the
fidelity-gate pattern, `sweep.py --unit conversation`, `offline_lib`, `cache_artifacts`.

New:
1. `chunker.py` typed-chunk functions (or a `--chunk-strategy` switch), behind a build
   flag so `build_index.py --chunk-strategy {S0..S3}` writes `embed-<arm>-<Sn>.db`.
2. Rollup stage in `search.py` (`dedup` | `aggregate`), env-knobbed like
   `SIFTD_HYBRID_STRATEGY`, so the fidelity gate can pin it and the replica mirror it.
3. Extend `offline_lib` replica for the rollup stage; re-run the fidelity gate per
   chunk-strategy (the replica must reproduce the engine for each new chunking).
4. Optional: a 6th ground-truth class targeting thinking/tool surfaces (for H-A2).

## Sequence + cost

Re-embedding is the only real cost (chunking is cheap; embedding ~200k chunks/arm is
not). Sequence to bound spend:

1. Prototype S0–S3 on the **local/bge arm** (free, ~hours each with the batch-cap fix).
2. Measure on local at conversation-unit; shortlist 1–2 chunk strategies + the rollup
   design.
3. Confirm the shortlist on **voyage** (cheap paid arm; the strong model where RRF is
   already competitive) — this is where a chunking win would promote RRF.
4. **gemini** only as a tie-breaker.

## Ratified decisions (fresh session, 2026-07-06)

1. **Rollup — dedup is the floor, not a candidate; aggregate is what we sweep.** dedup =
   "best chunk per conversation" is *not net-new*: `mmr_rerank` in `search.py` already
   hard-suppresses same-conversation chunks (two-tier penalty, penalty=1.0 when a
   chunk's conv is already selected). Lever B extracts that suppression into an explicit
   engine stage that *also* applies to the RRF path — a dissolution of duplicated
   behavior, making narrow and RRF symmetric by construction. It ships regardless.
   Neither variant needs pre-committing: both are post-retrieval arithmetic with **zero
   embed cost**, run offline on the same S1 index. Trap: `aggregate=max` is identical to
   dedup *for ranking* (rank-by-best-chunk = keep-best-chunk), so the only meaningful
   aggregate is **sum**, which re-introduces flooding (a 40-chunk mediocre conv sum-beats
   a 1-chunk great conv) unless *damped*. **Decision: build dedup as the mandatory stage,
   then sweep `{dedup, aggregate-damped}` offline. Damping — sweep a couple (top-k sum,
   mean) and report; no single form pre-chosen.**
2. **Event kinds to index — prompt + response only (S1).** Split the blended exchange
   into separate `prompt` and `response` chunks. Thinking and tool_call chunks are
   *deferred* — they add real embed cost and need a GT class to justify; they can be
   added later without rework. So stage 2 tests S0 (baseline) vs **S1 only**; S2/S3 drop.
3. **Chunk strategy granularity — deferred.** Only S1 vs S0 this round; no target_tokens
   sweep until S1 typing is shown to win.
4. **Paraphrase +20% margin — defer.** Re-ratify against results, not before.
5. **Per-preset vs global defaults — defer.** Let the S1+rollup data speak first.
6. **Ground-truth honesty** — keep labels at conversation granularity (as now) to stay
   strategy-agnostic. Unchanged.

### Arms
- **local (bge) + voyage only** this round. gemini dropped unless a tie-breaker is needed.

## Build status (2026-07-06, Phase 1 — code, no spend)

Landed on `feat/bench-stage1`:

- **S1 chunker** — `chunker.extract_typed_exchange_chunks`: per-exchange `prompt` +
  `response` chunks, windowed independently, each stamping its own `source_ids`
  (prompt_id / response_ids) so the FTS→chunk bridge survives. Reuses `fetch_exchanges`
  (prompt/response already separate there) — no new SQL. `tool_summary` unchanged.
- **Build flag** — `build_index.py --chunk-strategy {S0,S1}`; S1 writes
  `embed-<arm>-S1.db` (S0 keeps incumbent names so existing indexes resolve).
- **Rollup dissolved into the existing metric axis** — `--unit conversation` in
  `sweep.py` *already was* the dedup rollup (best chunk per conv), confirmed in
  `topn_convs`/narrow. So dedup is not net-new. Aggregate added as new `--unit` choices
  on the same axis (no separate `--rollup` knob): `conv-sum` (undamped flooding control),
  `conv-sum3` (top-3 damped), `conv-mean` (mean damped). Narrow collapses every non-slot
  unit to dedup (MMR already deduped it); aggregate does real work only on the RRF path.

Measured (406-conv deterministic sample of the snapshot): **corpus chunk amplification
S1/S0 ≈ 1.22×** (not the ~2× first feared — agentic convs have few real prompts and long
responses, and S0 already windowed the blended text). Projected S1 corpus ≈ 249k chunks
vs S0's 203k. Max chunks/conv rises (108→172 in-sample; real S0 max was 517), so flooding
pressure does increase — the rollup earns its place this round.

Phase 2 (compute): build S1 on local/bge (free) → sweep `{narrow,rrf} × {slot, conversation,
conv-sum, conv-sum3, conv-mean}` → shortlist → confirm on voyage (paid, gated).

### Lever B in isolation on S0 (free validation, local/bge arm)

Ran all five rollups against the *existing* S0 local index (no S1 build needed) — both
a code check and a real data point. best-RRF (H1) composite vs the fixed narrow80
baseline (0.4585), bge/local:

| rollup | best-RRF | Δ vs narrow | note |
|---|---|---|---|
| slot | 0.3249 | −0.1343 | flooding tax — RRF craters |
| dedup (`conversation`) | 0.4424 | −0.0161 | the fair baseline |
| **conv-sum3** | **0.4447** | **−0.0138** | best; top-3 damped sum |
| conv-mean | 0.4182 | −0.0404 | mean dilutes → worse than dedup |
| conv-sum | 0.4335 | −0.0250 | undamped → flooding returns (topical→0.000) |

Damping order is exactly the predicted physics: **sum3 > dedup > sum > mean**. Two reads:

1. **`slot`→`dedup` is the whole ballgame** (−0.118). That's the flooding tax, and dedup
   — already shipped via MMR — removes nearly all of it. Lever B's core value confirmed.
2. **Aggregate adds a hair at best.** Only conv-sum3 beats dedup, by +0.0023 composite;
   `sum` reintroduces flooding, `mean` dilutes. Per-class, aggregate helps identifier
   (0.875→0.902) and mixed (0.679→0.708) but *hurts paraphrase* (0.460→0.430) — multi-
   chunk evidence aids repeated-term classes, dilutes single-chunk semantic matches.

**S0 verdict: dedup is the workhorse; aggregate does not clearly earn its complexity on
S0.** The open question S1 tests: typed chunks are homogeneous (all-prompt / all-response),
which may give top-k-sum more clean signal — does conv-sum3's margin over dedup *grow*
under S1? If not, ship dedup and drop aggregate.

## The reframe

Stage 1 asked "how do we fuse two search signals." Stage 2 asks the question under it:
"what is the unit we index, score, and return." The dedup finding showed those are not
the same unit today, and the mismatch is where the RRF verdict lived. Settle the unit
(typed chunks + a rollup stage), then the fusion decision — and the per-preset and
margin questions — can be made on honest ground.
