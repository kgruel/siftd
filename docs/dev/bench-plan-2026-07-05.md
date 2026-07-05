# Bench plan — two-stage hybrid-search evaluation (pre-committed 2026-07-05)

Status: **ratified by user 2026-07-05.** Stage 1 authorized to run; stage 2 parked until
the user names their production embedding provider/tier. This document is the
pre-commitment artifact: the promote rules below were written **before** the sweep ran,
per the slice-4 lesson (the F3 gate worked because it was pre-committed — no
relitigating after seeing numbers).

## The two stages and the seam

- **Stage 1 — exploration, local, unbounded.** Full sweep on fastembed/bge-small
  (free, fast). Sweeps **rank-space** parameters only: rrf-k (per-list), keyword
  weight, MMR-λ, plus the H1-vs-H2 hypothesis rows. Rank-space parameters operate on
  orderings, which are far more stable across embedding models than raw scores — the
  shape of the response surface transfers.
- **Stage 2 — the binding gate, remote, narrow.** The 2–3 surviving configs re-run on
  the reference remote preset (= the user's production backend; API-first is the
  product default, so gating on local would bench a different product). **Score-space**
  parameters (similarity thresholds, first_mention 0.65) are raw-cosine-scale-dependent
  (bge-small compresses cosines into a narrow high band; other models distribute
  differently) and are calibrated in stage 2 **only**.
- **Transfer check built in:** stage 2 re-runs the full class suite on the shortlist.
  If the local config *ordering* inverts remotely, the rank-space transfer assumption
  is falsified → widen stage 2 rather than trust stage 1.

## Promote rules (pre-committed — do not adjust after data)

**RRF (any swept config) promotes over narrow-then-rank as hybrid default only if ALL:**

1. **Identifier class:** recall@10 within noise of narrow. "Within noise" = the
   bootstrap 95% CI (resampling over queries) of the per-query recall@10 delta
   includes zero or favors RRF.
2. **Paraphrase class:** recall@10 improves ≥ **+20% relative** over narrow.
   ⚠ **User note (2026-07-05): this margin specifically is flagged for revisit once
   the first sweep data exists.** Revisiting means re-ratifying a new margin with the
   user *before* using it to gate — not silently sliding it to fit a result.
3. **Topical class:** recall@10 no worse (same CI criterion as identifiers).
4. **Tool-findability class:** recall@10 no worse (this is the class that fired the
   slice-4 gate at −42%; it guards the same regression at scale).

**H2 (query-conditional keyword weight w(q)) promotes over H1 (single global operating
point) only if BOTH:**

- H2's composite (mean of per-class recall@10) beats H1's best config, AND
- H2 specifically wins on the mixed/partial-overlap query slice — the only place a
  conditional weight *can* earn its keep — by a margin large enough to justify runtime
  DF-signal machinery. Ties or unclear → **H1 wins by dissolution** (simpler).

**Metrics:** recall@10 gates. MRR is secondary/reported. avg_top1 is reported but
**never binding** (known structural inflation under full-set ranking; see
model-comparison-findings).

**Evaluation is per-class, never pooled** — pooling would let paraphrase gains launder
identifier regressions.

## Ground-truth classes (the persona gradient, easy → hard)

| Class | Persona | Construction | Labels |
|---|---|---|---|
| Identifier | stack-trace chaser | fts5vocab low-DF token mining over the corpus FTS; DF-stratified bands | exact by construction: the conversations containing the token |
| Tool-findability | "where did I edit X" | programmatic SQL over event_tool_call file basenames touched in 2–12 convs (ab_rrf pattern, scaled from 6 to ~100 queries) | the conversations that touched the file |
| Mixed / partial overlap | half-rememberer | paraphrase generation that *keeps* 1–2 mid-DF tokens while paraphrasing the rest | source conversation |
| Paraphrase | paraphraser | agent-generated natural queries from sampled chunks, **rare-token-overlap rejection** (any query sharing a low-DF token with its source chunk is discarded — otherwise FTS takes credit for semantic recall) | source conversation (+ chunk id secondary) |
| Topical | retrospective writer | conversation tags with 3–50 member convs, tag → natural query phrasing | tagged conversations |
| Miss-filing JSONL | disappointed searcher | post-ship longitudinal loop (search miss → labeled pair → regression test) | n/a for stage 1 — the channel that keeps the bench alive later |

Block-tag chunk-precision labels (PR #26 element tags bridging via source_ids) are a
stretch goal: chunk-level precision as a secondary metric if the bridge is cheap in
stage 1, else stage 2.

## Corpus and universe

**Full corpus, closed universe.** Index ALL substantive conversations, not a sample —
this dissolves the stratified-vs-recency sampling question entirely and closes the
universe (every FTS entrant is also in the vector index, so no since-bound hacks and
no out-of-universe entrants displacing top-10 slots, which was ab_rrf's structural
compromise).

- Main DB is **snapshotted** (APFS clone) — production is live; a moving corpus breaks
  determinism and label exactness.
- Embed DB is a scratch v2-schema build in `bench/runs/` (gitignored). Never touches
  production `~/.local/share/siftd/embeddings.db`.

## Sweep architecture

**Offline fusion over cached per-query artifacts.** For each query, fetch once: the
FTS ranked list (bm25), the full vector ranking (cosine over the whole chunk matrix),
and chunk embeddings (for MMR). The parameter grid then fuses offline — thousands of
configs in seconds, no re-embedding, no re-searching.

- **Fidelity gate (pre-committed):** the offline replicas of the two configs that
  exist as live engine knobs (narrow default, `SIFTD_HYBRID_STRATEGY=rrf` k=60) must
  reproduce the live engine's top-10 **exactly** on a probe set (~40 queries spanning
  classes) before any sweep result is trusted. Replica drift = fix first, no exceptions.
- **Grid:** weighted RRF `score = w_kw/(k_kw + r_kw) + 1/(k_vec + r_vec)` with k per
  list (keyword-k=60 dilution is the prime suspect from slice 4), w_kw swept, MMR-λ
  swept including off; narrow-then-rank swept over its own candidate-width knob if one
  exists. H1 rows = constant w_kw. H2 rows = pre-declared small family:
  step function on min-DF(query tokens) ≤ D → w_high else w_low, D ∈ {2, 5, 10, 20},
  (w_high, w_low) drawn from the H1 neighborhood. No post-hoc families.
- The runtime signal for H2's w(q) is the same fts5vocab DF machinery the dataset
  builder uses — identifier-shaped queries carry low-DF tokens by definition.

## Stage-1 run plan

1. Snapshot main DB; build full-corpus scratch embed index (background, CPU).
2. Mine identifier / tool-findability / topical ground truth programmatically.
3. Generate paraphrase + mixed classes via agents; apply rejection filters.
4. Fidelity gate on the live engine (env-knob configs only).
5. Cache per-query artifacts; run the grid; bootstrap CIs.
6. Report vs promote rules; shortlist 2–3 configs for stage 2.

Artifacts: `bench/stage1/` harness (committed on `feat/bench-stage1`),
`bench/runs/stage1-2026-07-05/` data (gitignored), results summary committed.

## Stage-2 dependencies (parked)

- User names production provider/tier (task #14 gates task #16).
- Score-space calibration (thresholds, first_mention) happens there.
- Small per-class confirmation samples are sufficient — stage 2 is confirmation, not
  exploration; respect provider rate limits (Voyage free tier = 3 RPM).
