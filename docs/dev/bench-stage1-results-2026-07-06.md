# Bench stage-1 results — three-arm cross-geometry sweep (2026-07-06)

> **⚠ SUPERSEDED IN PART — read this first.** The "RRF does not promote" headline below
> was measured at 10 chunk *slots*, which handicaps RRF: narrow-then-rank is MMR
> conversation-deduped but the RRF path is not, so one conversation's many matching
> chunks flood the slot budget. Re-scored at 10 distinct *conversations*
> (`sweep.py --unit conversation`, `sweep-report-<arm>-conversation.md`), RRF **wins
> composite on voyage and gemini** and passes all no-worse gates on voyage with
> paraphrase +10.3% (blocked from promotion only by the un-re-ratified +20% margin);
> it loses only on the weak bge arm. The honest conclusion is "RRF is better on strong
> embedders, worse on the weak one" — a model-dependent advantage — not "RRF loses."
> The slot-based tables below stand as recorded but should NOT be ratified; the
> conversation-unit results are the fair basis. See
> `bench-stage2-chunking-design-2026-07-06.md` for what comes next.


Companion to `bench-plan-2026-07-05.md` (the pre-committed plan + promote rules).
This is the results record; the promote rules were fixed **before** these numbers
existed and are not adjusted here. Raw data (gitignored): `bench/runs/stage1-2026-07-05/`
— `sweep-{results,report}-<arm>.{json,md}`, `fidelity-report-<arm>.json`.

## What ran

Full corpus, closed universe: **15,393 conversations / 203,133 chunks**, indexed
identically on three backends (same snapshot, same chunker — chunk count matches to
the row across all three arms).

| Arm | Backend | Dim | Provider |
|---|---|---|---|
| voyage | voyage-4-lite | 1024 | Voyage AI |
| gemini | gemini-embedding-001 | 3072 | Google |
| local | BAAI/bge-small-en-v1.5 | 384 | fastembed (local ONNX) |

632 ground-truth queries across five classes (identifier 110, tool 100, topical 16,
paraphrase 300, mixed 106). Each arm: fidelity gate (offline replica == live engine
top-10, **40/40 exact on both configs, all three arms**) → offline grid of 720 H1 +
24 H2 configs + narrow-recall baselines → paired bootstrap 95% CIs (1000 resamples,
seed 20260705) on per-query recall@10 deltas vs narrow recall=80.

## Headline: RRF does not promote on any arm

| Arm | Dim | narrow@80 | best H1 | best H2 | RRF promotes? |
|---|---|---|---|---|---|
| voyage | 1024 | **0.534** | 0.408 | 0.440 | no |
| gemini | 3072 | **0.519** | 0.401 | 0.405 | no |
| local | 384 | **0.459** | 0.325 | 0.360 | no |

Unanimous across three models from three sources spanning 384–3072 dimensions.
The identifier and tool "no-worse" gates fail (bootstrap CI strongly negative) for
every top RRF config on every arm. This replicates the slice-4 F3 gate finding at
full scale and across geometry. **RRF stays dormant; narrow-then-rank stays the
hybrid default.**

## Rank-space transfer: validated

Absolute retrieval quality tracks model strength — bge-small (384-dim) is weakest,
especially on tool and paraphrase:

| Arm | identifier | tool | topical | paraphrase | mixed |
|---|---|---|---|---|---|
| voyage | 0.841 | 0.325 | 0.009 | 0.617 | 0.877 |
| gemini | 0.852 | 0.307 | 0.009 | 0.623 | 0.802 |
| local | 0.845 | 0.209 | 0.000 | 0.507 | 0.736 |

…but the **relative** verdict (narrow > RRF, H1 > H2) is invariant. This is exactly
the plan's load-bearing prediction: rank-space parameters operate on orderings, which
transfer across embedding models even when raw scores do not. The shipped defaults
hold for arbitrary BYOK backends.

## H2 (query-conditional keyword weight) does not earn its keep

H2 beats H1 on composite on all three arms but must win the **mixed slice** — the only
place a conditional weight can justify its runtime DF machinery — to promote:

| Arm | mixed Δ (H2 − H1) |
|---|---|
| voyage | +0.000 (tie) |
| gemini | −0.047 |
| local | −0.009 |

Tie or loss everywhere → **H1 wins by dissolution** (simpler) on all three arms.

## The one actionable cross-arm divergence: the narrow recall knob

Composite by narrow recall width:

| Arm | r=40 | r=80 (ship) | r=160 | r=320 | optimal |
|---|---|---|---|---|---|
| voyage | 0.524 | **0.534** | 0.527 | 0.520 | 80 |
| gemini | **0.527** | 0.519 | 0.511 | 0.506 | 40 |
| local | **0.484** | 0.459 | 0.439 | 0.432 | 40 |

The shipped default is 80, but 2/3 arms prefer 40, and on gemini + local the curve is
**monotone decreasing** (40 > 80 > 160 > 320) — the weaker/differently-distributed
models want *tighter* FTS recall (less dilution before the embedding rerank). This is
a genuine "does not transfer identically" rank-space parameter and the first concrete
instance of the score-space → corpus/rank-relative reformulation flagged in the BYOK
amendment. **Open judgment item:** global 40, or per-preset calibration in
`embed_presets.toml`.

## Known non-informative floor: the topical class

Topical scores ~0 on all three arms (13/16 queries at recall@10=0 under every config,
including the narrow baseline) — **structural, backend-independent**, not a scoring
bug. Verified by hand: all curated tag-member conversations are in-universe, but the
tag→query map is literally `name.replace('-', ' ')`, so a 1–2-word tag phrase like
"dissolution" saturates FTS recall with hundreds of non-tagged conversations that use
the word, and the tagged subset can't float after rerank. Editorial tags
("inciting-friction") encode judgment *about* conversations, not words *in* them —
an orthogonal retrieval channel content search cannot reach (an argument for the tag
filter, not a search deficiency). Its promote-rule "no-worse" CI passes **trivially**
(both sides pinned at floor); do not read that as RRF signal.

**Open judgment item:** exclude topical from composite/gating for this run (annotate as
structurally-invalid ground truth, keep visible per-class); regenerate topical queries
with richer phrasing as a follow-up if the class should discriminate.

## Open items for the judgment session

1. **Topical handling** — exclude from composite/gate this run; regen queries later.
2. **Narrow recall default** — 80 is voyage-tuned; 2/3 arms want 40. Global vs per-preset.
3. **Paraphrase +20% margin** — moot both directions: no config approaches +20% (best
   is gemini +12.3%), and those configs fail the identifier gate independently. The
   margin re-ratification does not change any verdict.
4. **Ratify RRF-stays-dormant** across three arms; decide whether
   `test_hybrid_strategy_default_is_narrow` stays as-is (it does — RRF did not earn the
   flip).
5. **Chunking strategy** — the three arms now provide the substrate to explore chunk
   granularity/overlap effects on retrieval, separate from the fusion question.

## Method notes (review items)

- **recall@10** mirrors `ab_rrf.py`: the first 10 result *slots* projected to their
  distinct conversation set, scored `|got ∩ labels| / min(10, |labels|)`. RRF pays for
  same-conversation duplicate chunks consuming slots. Documented in `sweep.py`.
- **Fidelity** is at conversation granularity (the granularity recall@10 scores at) —
  "exact" means exact-at-conversation-level over the top-10 chunks.
- **avg_top1** reported but never binding (structural inflation under full-set ranking).
