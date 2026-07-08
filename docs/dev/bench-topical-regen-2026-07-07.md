# Bench topical-class regeneration — off the floor (2026-07-07)

Companion to `bench-stage1-results-2026-07-06.md` ("Known non-informative floor: the
topical class") and `bench-stage2-chunking-design-2026-07-06.md`. Fixes the structurally
dead topical ground-truth class so it discriminates across configs, instead of passing
its no-worse promote gate vacuously (both sides pinned at recall@10≈0).

## The problem (recap)

The topical miner mapped every curated tag to its slug: `name.replace('-', ' ')`. Stage 1
found 13/16 of those queries scored recall@10=0 under **every** config including the
narrow baseline — backend-independent, all three arms. Two failure modes were tangled
together: (a) 1–2-word tag phrases (`dissolution`, `architecture`, `cli`) saturate FTS
with hundreds of non-tagged conversations, so the tagged subset can't float after rerank;
(b) editorial tags (`inciting-friction`) encode judgment *about* a conversation, which
content search cannot reach at all. A class where the gate can only pass trivially tells
us nothing about RRF vs narrow.

## Diagnosis — content-reachable vs judgment-only

The discriminating property is **content-reachability**: a tag can be rescued only if its
*slug's plain meaning names something the conversation literally discusses*, so a natural
query a retrospective-writer would type reaches the members through content (FTS or
vector). I measured, per tag on the local/bge arm: FTS recall + label-hit rank, and the
best vector-cosine rank of any label conversation (`vec@`, a pure reachability signal
independent of query phrasing quality). I also inspected member prompts to confirm the
call (diagnosis-only; it did **not** feed the query text — see the honesty note).

| tag | n | vec@ (best label rank) | verdict | why |
|---|---|---|---|---|
| the-great-deletion | 7 | **0** | **keep** | body = framework decouple/cleanup, deleting example code — slug names it |
| principles:architecture | 4 | **2** | **keep** | body = domain-objects/protocol refactor, storage seam |
| principles:cli | 3 | **9** | **keep** | body = CLI + `doctor` command work |
| vocabulary-as-architecture | 5 | **40** | **keep** | body = vocabulary/type-rename refactor (ev→facts, shapes) |
| self-similarity | 4 | 58 | drop | abstract framing over concrete ticks/vertex work; slug ≠ content |
| dissolution | 11 | 77 | drop | the user's *design principle*, applied across arcs; not a topic in the body |
| co-creation | 5 | 78 | drop | names the collaboration mode, not a retrievable topic |
| observation-as-participation | 3 | 135 | drop | philosophical framing over unrelated ev work |
| the-missing-middle | 7 | 166 | drop | abstract "missing layer" framing over project reviews |
| research:principles | 8 | 847 | drop | "design principles" — too generic, saturates |
| inciting-friction | 4 | 769 | drop | the tension that started an arc — pure judgment |
| forcing-function | 8 | >2k | drop | warmup-bodied convs, vector-unreachable |
| research:browser-tool | 4 | >2k | drop | coordination-noise body (teammate shutdown JSON); unreachable |
| rationale:source-boundary | 4 | >2k | drop | same coordination-noise body |
| research:token-efficient-browsing | 4 | >2k | drop | same coordination-noise body |
| research:agent-team-patterns | 4 | >2k | drop | same coordination-noise body |

The four `research:*` / `rationale:*` tags on the same 4 conversations (01KGWNA4…) share
one coordination-noise body — those conversations open with agent shutdown messages and
never discuss the tagged topic in retrievable prose, so they are unreachable at any
phrasing. That is an argument for the *tag filter*, not a search deficiency.

**Split: 4 kept, 12 dropped.** A class of 4 live, discriminating queries is more honest
than 16 mostly-dead ones.

## Design — richer query generation

The kept tags carry a static gloss (`ground_truth.TOPICAL_GLOSS`), e.g.
`principles:cli` → *"designing the command-line interface and implementing the doctor
health-check command"*. Properties, against the task constraints:

- **Reproducible by construction.** A static dict — no `random`, no `Date.now`, no seed.
  The most reproducible form possible; bench reruns are identical.
- **No label leak beyond user knowledge.** Query text is derived from the tag slug's plain
  meaning plus the user's own project vocabulary (ev / prism / ticks / siftd / tbd) — what
  a user who *coined* the tag knows. It is not built from member content. The
  reachability diagnosis (vec@, prompt inspection) decided keep/drop only; it never
  authored query text.
- **Labels stay conversation-granular.** Unchanged from the original miner.

Dropped tags are recorded in `TOPICAL_DROP` with a one-line rationale each, so a re-mine
over a changed snapshot warns on any *unclassified* tag rather than silently regressing to
the dead phrasing.

## Validation — local/bge arm, unit=conversation

Scored offline against the cached local arm (`embed-local.db`, free — local ONNX bge
embeddings), at conversation-unit (the stage-2 dedup rollup), across a representative
config grid (narrow80 + 6 RRF points spanning pool/λ/w/k). Before = original slug queries,
after = glosses, both on the **same 4 kept tags**:

| | narrow80 | RRF mean (grid) | RRF best | off-floor | spread across configs |
|---|---|---|---|---|---|
| **before** (slug) | 0.000 | 0.024 | 0.036 | 1/4 | 1/4 |
| **after** (gloss) | 0.246 | 0.154 | — | 3/4 | 3/4 |

Per-tag after (gloss), RRF best / narrow80: architecture 0.50 / 0.50, great-deletion
0.43 / 0.29, cli 0.33 / 0.00, vocabulary 0.00 / 0.20. The class comes **off the floor and
spreads across configs** — the success criterion. `vocabulary-as-architecture` discriminates
on the narrow/vector path (0.20) but RRF's keyword arm floods it to 0; it is content-reachable
(vec@40) and kept, but it is the weakest of the four.

## Verdict impact — nothing flips

The topical class enters the sweep only via (1) the composite (mean of 5 class means) and
(2) the identifier/topical/tool **no-worse** promote gate (RRF's per-query recall@10 CI vs
narrow80 must have `hi ≥ 0`). Measured at the stage-2 winner config family
(`w1.0_kkw20_kvec20_p300_l1.0`), paired bootstrap over the 4 queries (seed 20260705, 1000
resamples):

| | topical narrow80 | topical RRF(winner) | Δ(RRF−nar) | CI | no-worse met? |
|---|---|---|---|---|---|
| old (dead) | 0.000 | 0.036 | +0.036 | [+0.000, +0.107] | yes (vacuous) |
| **new** | 0.246 | 0.170 | −0.077 | [−0.225, +0.071] | **yes (hi ≥ 0)** |

The class now leans narrow > RRF on topical, but with n=4 the CI comfortably includes 0,
so the **no-worse gate still passes** — no promotion decision flips. Composite rises in
parallel for both paths (topical goes ~0 → ~0.2 for narrow and ~0.17 for RRF), shifting
the local RRF−narrow composite delta ~−0.02 more negative — which only *reinforces* the
existing bge verdict (weak arm → narrow) rather than changing it. The stage-1 RRF-stays-
dormant and stage-2 per-preset / dedup-on-RRF verdicts are unaffected.

## Open judgment calls (for user review)

1. **n=4 is thin for a binding gate.** Topical now discriminates but its CI is wide. If
   topical is to *gate* promotion (not just be visible), 4 queries under-powers it; the
   honest options are (a) treat topical as reported-not-gating, or (b) broaden the corpus
   with more content-reachable tags before relying on it. I recommend (a) for this run.
2. **Strong-arm confirmation not yet run.** Before/after and the gate check were done on
   local/bge (free). Folding the new topical into the composite requires re-caching
   `artifacts-<arm>` (the shared `queries.jsonl` still holds the old 632-query set incl.
   old topical) and re-running the sweep at `--unit conversation`. I did **not** touch the
   shared artifact cache (another agent shares the worktree). On voyage the narrow>RRF
   topical lean could be sharper; worth a confirm if topical will gate.
3. **`vocabulary-as-architecture` is marginal** (RRF 0.00, narrow 0.20). Keep it for the
   narrow/vector signal, or drop to a clean 3-tag class of RRF-discriminators. My call:
   keep — it is content-reachable and the narrow-path signal is real.
4. **Query count drops 632 → 620.** Cosmetic, but any doc citing "632 ground-truth queries"
   should be updated when the new topical is folded into a full re-sweep.

## Files

- `bench/stage1/ground_truth.py` — `TOPICAL_GLOSS` + `TOPICAL_DROP` + rewritten
  `mine_topical` (static, deterministic; warns on unclassified tags).
- `bench/runs/stage1-2026-07-05/gt-topical.jsonl` — regenerated (4 queries). Gitignored data.
- Re-fold to composite: re-run `cache_artifacts.py --backend local` (forcing re-embed of
  the changed query set) then `sweep.py --backend local --unit conversation`. Not done here
  to avoid clobbering the shared cache.
