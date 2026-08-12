# `/simplify` agent prompts

Read this when launching the four review agents. Copy the preamble verbatim
into each of the four prompts and append that agent's angle — the boilerplate
is the part that is mechanical to include and catastrophic to omit.

**Commit and push the branch first.** The preamble's checkout is what makes the
agent see a diff at all.

## Shared preamble

Substitute `<branch>` and the one-paragraph `<context>` describing what the
change does.

```
You are reviewing a diff in the siftd repo for **<ANGLE>** issues only.
Quality review, NOT bug hunting.

FIRST, run these literally — your worktree starts at the base commit, so
without this you will review an empty diff:
```
git fetch origin
git checkout -B review-local origin/<branch>
git diff origin/main...HEAD
```

TIME BUDGET: finish well under 10 minutes. Return partial findings rather than
nothing. Do NOT run `./dev setup` or a full test suite; a targeted
`.venv/bin/python -m pytest <file> -q -n 0` is fine if the venv already exists.

CONTEXT: <context>

YOUR ANGLE — <ANGLE>: <angle body, below>

<3-5 bullets of "specifically worth checking", named files and functions from
this diff. These are what make the difference between a generic review and one
that finds something.>

Report each finding as: file, line, one-line summary, and the concrete cost.
Return findings as text — do not fix anything.
```

Launch all four in one message with `isolation: "worktree"` so they run
concurrently.

## The four angles

**Reuse** — Flag new code that re-implements something the codebase already
has. Grep shared/utility modules and files adjacent to the change, and name the
existing helper to call instead.

**Simplification** — Flag unnecessary complexity the diff *adds*: redundant or
derivable state, copy-paste with slight variation, deep nesting, dead code left
behind. Name the simpler form that does the same job. Worth asking explicitly:
is any residue left by something the change dissolved?

**Efficiency** — Flag wasted work the diff introduces: redundant computation,
repeated I/O, independent operations run sequentially, blocking work added to
startup or hot paths. Also long-lived objects built from closures, which keep
the entire enclosing scope alive.

Add to this one: *"If you benchmark, say exactly what you measured and how. Do
NOT report a speedup you cannot reproduce twice, and beware warm-cache
contamination."* Without it you get numbers you cannot use — on #34 the agent
reported a 4× speedup from a change that measured ~18% when reproduced.

**Altitude** — Is each change at the right depth, or a fragile bandaid? Special
cases layered on shared infrastructure signal the fix isn't deep enough.

This one earns its keep when you give it the repo's stated principles
(dissolution test, substrate-first, "the api layer is the boundary") and ask it
to **argue both sides** of the placement question. On #32 that produced the
finding that the fix sat one layer too shallow, with the counter-argument
stated fairly enough to act on.

## What to expect back

- Findings are text; you apply them in the real tree. Anything an agent "fixed"
  in its worktree is discarded — which is what you want from a reviewer.
- Reproduce any measurement yourself before citing it.
- Two agents finding the same thing independently is the strongest signal in
  the pass. On #32 reuse and altitude both found a whole input context the fix
  had missed.
