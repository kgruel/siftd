# Narrow-path MMR-λ sweep — post-dedup-rollup (2026-07-07)

Status: **answered, no change recommended.** Companion to
`bench-stage2-chunking-design-2026-07-06.md` (the dedup-rollup arc this question follows
from).

## Question

Now that conversation-dedup is structural (shipping on both narrow and RRF paths), is
`mmr_rerank`'s shipped λ=0.7 (`src/siftd/search.py:127`, `offline_lib.DEFAULT_LAMBDA`)
still right for the **narrow** path? Hypothesis: MMR's diversity penalty was partly doing
dedup work; with dedup now a structural stage, λ closer to 1.0 (pure relevance, no
diversity penalty) might win.

## Step 1 — was this already swept?

No. `sweep.py`'s `LAMBDAS = ["off", 0.5, 0.7, 1.0]` grid (line 90) only varies λ on the
**RRF/H1 fusion path** — every `(pool, λ)` combo in that grid feeds `build_universe` →
the RRF universe. The narrow baseline is computed separately (`sweep.py:516-526`) via
`replica_narrow(arm, qv_list, recall_ids, recall=r)` — called with **no `lambda_` kwarg**,
so it always runs at `offline_lib.DEFAULT_LAMBDA = 0.7` (mirroring `search.py`'s shipped
default) across all four `NARROW_RECALLS` values. λ was never varied for narrow anywhere
in stage 1 or 2. The question was open as stated.

## Step 2 — method

New script: `bench/stage1/lambda_sweep_narrow.py`. Reuses `offline_lib.ArmData` +
`replica_narrow` unchanged, holds narrow's candidate-width knob fixed at the shipped
value (`recall=80`), and sweeps `lambda_ ∈ {0.5, 0.7 (shipped), 0.85, 0.95, 1.0}` over
all 632 cached queries. Both arms use **cached artifacts only** — `artifacts-local` and
`artifacts-voyage` under `bench/runs/stage1-2026-07-05/`, no new embedding calls. Metric
is recall@10 (ab_rrf definition, `sweep.py`'s own), scored at conversation-unit: narrow's
MMR output is dedup'd chunk→conv same as `sweep.py`'s `unit=conversation` path (its own
comment at line 520-522 establishes this equivalence — narrow's MMR already conv-dedups
its output, so `unit=conversation` collapses to the same list as `unit=slot` deduped).
Bootstrap CIs: 1000 resamples, same seed (20260705) as the stage-1/2 sweep, per-class,
paired against the λ=0.7 row.

Caveat: this is the offline replica, not a full fidelity re-gate (no live-engine
side-by-side re-run at each λ). The replica was already gate-proven equal to the engine
at λ=0.7/DEFAULT_RECALL in stage 1; the same code path is reused here with only λ varied,
so drift risk is low, but it's not re-certified per point.

## Results

### local / bge (weak arm)

| λ | composite | identifier | tool | topical | paraphrase | mixed |
|---|---|---|---|---|---|---|
| 0.5 | 0.4467 | 0.850 | 0.199 | 0.000 | 0.477 | 0.708 |
| **0.7 (shipped)** | 0.4592 | 0.845 | 0.209 | 0.000 | 0.507 | 0.736 |
| 0.85 | **0.4678** | 0.828 | 0.208 | 0.000 | 0.520 | 0.783 |
| 0.95 | 0.4455 | 0.762 | 0.197 | 0.000 | 0.513 | 0.755 |
| 1.0 | 0.4248 | 0.708 | 0.174 | 0.000 | 0.507 | 0.736 |

### voyage (strong arm)

| λ | composite | identifier | tool | topical | paraphrase | mixed |
|---|---|---|---|---|---|---|
| 0.5 | 0.5175 | 0.844 | 0.306 | 0.009 | 0.590 | 0.840 |
| **0.7 (shipped)** | **0.5333** | 0.841 | 0.325 | 0.009 | 0.613 | 0.877 |
| 0.85 | 0.5303 | 0.822 | 0.327 | 0.009 | 0.617 | 0.877 |
| 0.95 | 0.5119 | 0.767 | 0.309 | 0.009 | 0.597 | 0.877 |
| 1.0 | 0.4913 | 0.700 | 0.299 | 0.009 | 0.590 | 0.858 |

Paraphrase CI vs λ=0.7 (voyage): λ=0.5 −0.023 [−0.043,−0.007]; λ=0.85 +0.003
[−0.010,+0.020]; λ=0.95 −0.017 [−0.040,+0.003]; λ=1.0 −0.023 [−0.047,+0.000]. Paraphrase
never significantly improves at higher λ on either arm — the CI at λ=0.85/0.95/1.0
straddles zero or trends negative, opposite the "pure relevance helps paraphrase" framing.

## Verdict

**Hypothesis falsified, direction reversed. Keep shipped λ=0.7.** λ=1.0 is clearly worse
on both arms (bge: −0.034 composite; voyage: −0.042 composite), driven almost entirely by
**identifier** collapsing (bge 0.845→0.708, voyage 0.841→0.700) — the largest, most
reliable-recall class taking the biggest hit. λ=0.85 edges 0.7 on bge (+0.0086 composite,
mostly paraphrase +0.013 and mixed +0.047) but is a hair *worse* than 0.7 on voyage
(−0.003, within noise) — not a clean win on the arm that matters most for the per-preset
default.

**Why the hypothesis was backwards**: dedup (best-chunk-per-conversation) can only
surface conversations that already made it into MMR's *pre-dedup* top-10 chunk list.
MMR's diversity penalty is what pushes distinct conversations into that pre-dedup window
in the first place — without it (λ→1), the top-10 *chunks* skew toward a handful of
conversations with many near-duplicate high-cosine chunks (exactly the class of query
this system is strong at: repeated identifiers/tool names recur densely within a few
conversations). Dedup then has fewer distinct conversations to project onto 10 slots, so
recall drops. The dedup rollup and MMR's diversity term are not redundant — they solve
different problems: MMR generates conversation diversity in the candidate pool; dedup
projects a chunk-ranked list onto the conversation unit. Removing MMR's diversity
component starves dedup of material to work with. This mirrors the RRF-side finding
(`slot`→`dedup` was the flooding fix) from the other direction: on narrow, the "flooding
fix" was never separate from MMR — it's already inside the λ=0.7 MMR call, which is why
narrow never needed an explicit rollup stage while RRF did.

**No engine change recommended.** `mmr_rerank`'s default (`src/siftd/search.py:127`) and
`offline_lib.DEFAULT_LAMBDA` stay at 0.7 for the narrow path. (0.85 is arguably a
free-arm-only micro-win not worth chasing given it's a wash on voyage and per-class
tradeoffs are small.)

## Artifacts

- `bench/stage1/lambda_sweep_narrow.py` — the sweep script (kept, reusable).
- `bench/runs/stage1-2026-07-05/lambda-sweep-narrow-{local,voyage}.json` — raw results
  (gitignored).
