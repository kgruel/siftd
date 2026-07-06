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

## Open decisions for the fresh session (do not pre-decide)

1. **Rollup: dedup vs aggregate**, and is it the correct *default* final stage for ALL
   strategies (likely yes — users want conversations)? If yes, it lands independent of
   the RRF question.
2. **Which event kinds to index** — thinking chunks add real embed cost (thinking is
   voluminous); worth it only if a class targets them.
3. **Chunk strategy granularity** — how many variants to actually re-embed (cost).
4. **Re-ratify the paraphrase +20% margin** on the fair stage-1 numbers *before* stage 2
   layers on — on voyage RRF misses promotion only by this margin today.
5. **Per-preset vs global defaults** — RRF-for-strong / narrow-for-weak is already on
   the table from the fair re-run; stage 2 may dissolve it (if rollup makes RRF
   robust) or confirm it.
6. **Ground-truth honesty** — typed chunks change what "the answer chunk" is; keep
   labels at conversation granularity (as now) to stay strategy-agnostic.

## The reframe

Stage 1 asked "how do we fuse two search signals." Stage 2 asks the question under it:
"what is the unit we index, score, and return." The dedup finding showed those are not
the same unit today, and the mismatch is where the RRF verdict lived. Settle the unit
(typed chunks + a rollup stage), then the fusion decision — and the per-preset and
margin questions — can be made on honest ground.
