# Dogfood Validation Report — Dissolution Genesis Brief

**Task:** Trace the genesis of the "dissolution test" concept in Kyle's conversation history.  
**Approach:** Start broad (FTS5 search), narrow to earliest tagged conversations, drill into genesis conversation, trace forward to current form.  
**Embeddings:** Not installed (`[i] embeddings-available` in doctor output — embeddings DB exists but `[embed]` extra absent). All search ran in FTS5 keyword mode.  
**Siftd binary:** `.venv/bin/siftd 0.8.1` (same version as globally installed, per `--version`).

---

## Commands Run

### 1. `.venv/bin/siftd --version`
Expected: confirm it's the local build. Got: `siftd 0.8.1`. Same version as global — can't A/B on version, but the binary resolves correctly to the venv path.

### 2. `.venv/bin/siftd --help`
Orientation. Confirmed available commands. No surprises.

### 3. `.venv/bin/siftd query -s "dissolution test" --limit 20`
Error: `unrecognized arguments: -s`. Reached for `-s` as a natural search flag shorthand. Doesn't exist. Had to pivot to `siftd search` for content search vs `siftd query` for metadata/filter. The split is logical once you know it, but `-s/--search` as a shortcut on `query` would be a natural first reach.

### 4. `.venv/bin/siftd query --help`
Confirmed: `query` is metadata/filter only; content search lives in `siftd search`.

### 5. `.venv/bin/siftd search --help`
Understood `--select`, `--sort`, `--mode`, `--fts`, `--semantic` flags. Good. Ready to search.

### 6. `.venv/bin/siftd search "dissolution test" --fts -n 15`
**First signal.** Top hits:

| Conv ID | Date | Workspace | Snippet |
|---------|------|-----------|---------|
| 01KH01M6 | 2026-02-07 | questions | User: "I think the dissolution test is perhaps something we should stick in our global memory" |
| 01KH01M8 | 2026-02-07 | Obsidian | "Sometimes the dissolution test yields 'just pay for the good product.'" |
| 01KJY4NW | 2026-03-04 | meta-discussion | "This is a dissolution test — does the migration strategy itself dissolve?" |

Earliest visible date: **2026-02-07** in `questions` workspace. The "stick in global memory" phrase tells me this is the *naming moment*, not the genesis itself.

### 7. `.venv/bin/siftd search "dissolution test" --fts --select=first --sort=time`
Warning: `--select=first ignored in FTS5 mode (requires embeddings)`. Got same results, still sorted by relevance score. The warning is clear and actionable, but the fallback-to-relevance behavior means chronological search is unavailable without embeddings — this is a real capability gap for tracing genesis questions.

### 8. `.venv/bin/siftd search "dissolve" --fts --select=first --sort=time`
Same `--select=first` warning. Earliest visible hit: `01KJ71JM` from 2026-02-24. But this can't be the earliest because the `#dissolution` tag appears on Jan 4 conversations. The FTS5 index is either missing content from those early conversations, or those conversations don't use the word "dissolve" in text.

### 9. `.venv/bin/siftd query -l dissolution --oldest -n 20`
**Key pivot.** Used tag-based query instead of content search. This bypassed the FTS5 index gap and found:

| ID | Date | Workspace |
|----|------|-----------|
| 01KFMBER | 2026-01-04 | ev |
| 01KFMBES | 2026-01-04 | ev |
| 01KFV90V | 2026-01-25 | experiments |
| 01KFW0V6 | 2026-01-25 | cells |
| 01KFYDMR | 2026-01-26 | prism |
| 01KG0815 | 2026-01-27 | prism |
| 01KGWNA4 | 2026-02-07 | questions |

Earliest: **Jan 4, 2026** in `ev` workspace — a full month before the "naming" conversation. Tag was clearly applied retroactively.

### 10. `.venv/bin/siftd query 01KFMBER --summary` and `.venv/bin/siftd query 01KFMBES --summary`
The Jan 4 ev conversations: 27 and 24 prompts, 64k and 52k tokens. Large sessions. The workspace is `/Users/kaygee/Code/ev`.

### 11. `.venv/bin/siftd query -l dissolution --oldest -n 20 --json` (parsed with python3)
Got full IDs. Both Jan 4 conversations started in the `ev` workspace. Tagged `dissolution` but no content in those conversations uses that word — the tag was applied retroactively (see genesis doc conversation below).

### 12. `.venv/bin/siftd query 01KH01M6 -F` (76-turn conversation, "questions" workspace)
The Feb 7 conversation where the naming moment happens. This is a long session. Searched for "dissolution" in the output. Key sections:

- **Line ~1867 (13:12 timestamp):** A `siftd` sidecar teammate message arrives. The sidecar had been sent to retrieve dissolution pattern examples. It returns a report titled "Dissolution Pattern and Source Boundary Decisions" with concrete examples: Tick-to-Fact dissolution (Jan 27), persistence concepts dissolved (Jan 28), Peer dissolved into identity string (Jan 31).

- **Line ~1961:** The sidecar's report articulates the test: *"The dissolution test is: if you can express X as a property, field, or composition of existing atoms, X doesn't get to be an atom. The project applies this ruthlessly — 5 atoms became 3, persistence became 'facts all the way down', and Source became 'just run shell commands.'"*

- **Line ~2240 (13:40 timestamp):** Kyle says: *"I think the dissolution test is perhaps something we should stick in our global memory, yeah? Perhaps a very concise 'apply the dissolution' test like the explicit over implicit."*

- **13:40 — Assistant:** Reads `~/.config/claude/CLAUDE.md` and adds the line under Preferences.

The naming moment is an LLM retroactively naming a pattern it retrieved from tagged history and presenting it in crystallized form.

### 13. `.venv/bin/siftd query 01KG8EWGAWDF --summary`
The "genesis doc" conversation referenced by the sidecar's report. Started **2026-01-27 11:21** in `prism` workspace. 6 turns, 1.7k tokens. This is where the dissolution pattern was first formalized.

### 14. `.venv/bin/siftd query 01KG8EWGAWDF -F`
Output was too large (75.5KB) and persisted to file. Read via shell `cat`. Key sections:

The user asked the agent to (1) develop a genesis document for the prism project, (2) tag interesting concepts, (3) give tool feedback. The agent used the strata skill to search past conversations and surface thematic clusters. The genesis document describes the dissolution pattern in three acts:

- `#dissolution` as one of 10 tags developed for semantic clustering
- Examples: "Stream as a concept disappears. It was plumbing pretending to be architecture." / "Scope → Horizon + Potential" / "Should Tick collapse into Fact?"
- The summary: "Can this concept be expressed as a property or composition of existing atoms? If yes, it dissolves."

The `#dissolution` strata tag was then applied to 10 conversations spanning Jan 4–Jan 27.

This is the **first explicit named formulation** of the dissolution concept — but it names a pattern that had been practiced since Jan 4 (in ev) without a name.

### 15. `.venv/bin/siftd query 01KG8EWJ4AZW --summary` and `01KG8EW9J9QC --summary`
Supporting dissolutions:
- **01KG8EWJ4AZW** (Jan 27, 09:40): "The question dissolved. There is no tick-to-fact conversion. The framing was wrong." — Tick-to-Fact dissolution
- **01KG8EW9J9QC** (Jan 28): Persistence dissolution — Sink/Store/Witness/Memory all fold into existing concepts

### 16. `.venv/bin/siftd search "dissolution test before building" --fts -n 10`
Traced forward development. Key hits:

| Conv ID | Date | Significance |
|---------|------|--------------|
| 01KKFSQ7 | 2026-03-09 | "The dissolution test applied to my own proposal — 'can this be expressed as what already exists?' Yes. Facts." |
| 01KM37XV | 2026-03-19 | First explicit citation of dissolution test as a CLAUDE.md principle in siftd context |
| 01KQAC6M | 2026-04-28 | Worker brief includes "dissolution test before building" as a constraint |
| 01KR5248 | 2026-05-08 | Constraints specify: "Apply dissolution test BEFORE proposing any new type/protocol/function" |

By March, "dissolution" is working vocabulary. By April-May, it's appearing verbatim in agent briefs as a constraint.

### 17. `.venv/bin/siftd doctor`
System health. Output:
- `[!] ingest-errors` for claude_code (8 files) and gemini_cli (31 files)
- `[i] embeddings-available: Embeddings database exists but embedding support not installed`
- Various pending ingestion (14 claude_code files, 7 codex, 2 vscode)
- 3 active sessions older than 48h with pending tags

The embeddings-stale caveat fired as `[i]`-level info, not a warning. The hint says "install embeddings for semantic ranking: siftd install embed."

---

## Answer: Genesis of the Dissolution Test

**The practice preceded the name by ~3 weeks.**

### Phase 1: Unnamed practice (Jan 4, 2026)
In the `ev` workspace, proposed abstractions (EventKind variations, status event kinds) get pressure-tested against existing atoms. The pattern is there — "can this be a property of what already exists?" — but no vocabulary for it yet.

### Phase 2: Pattern accumulates across projects (Jan 4–31)
Across `ev`, `experiments`, `cells`, `prism`: Tick dissolves (temporarily, then reinstated), Peer dissolves into identity string, persistence concepts (Sink/Store/Witness/Memory) dissolve into existing primitives, Source dissolves into "just run shell commands." Five atoms become three. Each is a dissolution event without the name.

### Phase 3: First formal naming (Jan 27, 2026, 11:21)
Conversation **01KG8EWGAWDF** — a "genesis doc" session for the prism project using the strata skill. An agent retrieves past conversations, identifies the recurring pattern, and names it in a genesis document. The `#dissolution` tag is applied retroactively to 10 conversations. The test formulation: *"Can this concept be expressed as a property or composition of existing atoms? If yes, it dissolves."*

### Phase 4: The naming moment in conversation (Feb 7, 2026, 13:12)
Conversation **01KH01M6** — Kyle is asking "should we build a browsing tool?" A siftd sidecar agent is running alongside. It retrieves the prism dissolution history and returns a report with the crystallized formulation. The assistant applies the test in real-time: "the browsing tool dissolves — it's `curl | readability | markdown`, a shell command a Source can call." The concept becomes a live decision tool, not just a retrospective label.

### Phase 5: Enters global memory (Feb 7, 2026, 13:40)
Kyle explicitly requests: *"I think the dissolution test is perhaps something we should stick in our global memory."* The assistant adds to `~/.config/claude/CLAUDE.md`:
> "Dissolution test before building—can X be expressed as a property or composition of what already exists? If yes, it dissolves. (siftd: `dissolution`, `rationale:source-boundary`)"

### Phase 6: Sharpened to "before, not after" (May 8, 2026)
After caveats slice 1 restart — where 5 of 6 shipped pieces had clean dissolution paths that weren't applied upfront — the memory `dissolution-before-building.md` is added. Key refinement: *"I treated it as a check rather than a prereq."* The principle gains the temporal constraint: dissolution analysis is not post-hoc review but pre-design prerequisite. The anti-pattern is documented with measured cost (22 files churn, 5 user-caught regressions).

### Current Form
- **Global CLAUDE.md:** "Dissolution test before building—can X be expressed as a property or composition of what already exists? If yes, it dissolves."
- **Memory `dissolution-before-building.md`:** Full anti-pattern documentation, 5 application checkpoints, reasoning for why post-hoc dissolution is worse than upfront.
- **In practice:** appears verbatim in worker briefs as a constraint; applied routinely in design sessions as "dissolution confirmed" / "clean dissolution."

---

## Friction Notes

**1. `-s` / `--search` as a query shorthand.** I reached for `siftd query -s "dissolution test"` before knowing search is a separate subcommand. Not a bug — the split is principled — but `-s` is a very common CLI shorthand that fails silently with "unrecognized argument" rather than "did you mean: siftd search?". A hint in the error would help.

**2. `--select=first` warning but no fallback.** When FTS5 is active, `--select=first` (chronological earliest match) silently degrades to relevance ordering with a warning. For genesis-tracing tasks this is the main limitation. The warning is good. But there's no FTS5 equivalent of `--sort=time` that would get me to "oldest matching conversation" without embeddings. I had to pivot to tag-based queries (`-l dissolution --oldest`) to get chronological ordering. That's the right tool, but it requires knowing the tag exists.

**3. FTS5 coverage gap.** `siftd search "dissolve"` finds nothing in ev or prism workspaces even though those conversations clearly contain instances of the pattern. Either the early conversations aren't indexed in FTS5, or the vocabulary used there doesn't match. The genesis conversation (01KG8EWGAWDF) was 75.5KB and hit a persisted-output path — that output path worked fine, but it raised a question: are oversized conversations indexed differently?

**4. Content search vs. tag search as different research strategies.** The most productive pivot in this session was switching from content search to tag-based query (`-l dissolution --oldest`). This got me to Jan 4 conversations that FTS5 missed entirely. The two approaches are complementary, but nothing in the CLI surface hints that "the tag knows what the FTS index doesn't." This is a power-user insight that isn't surfaced anywhere.

**5. Conversation ID vs chunk ID mismatch.** Search results show 8-char IDs that identify the conversation. Drilling into a search result requires taking that ID and running `siftd query <id>`. The workflow is: search → see hit → note conv ID → run query. The flow is clear once you know it; the `--context` flag suggests it (but `--context` is ignored without embeddings). The examples in `--help` cover this, but an agent that's running purely from search results has to know the convention.

**6. `--context` and `-v` silently ignored in FTS5 mode.** Both suppressed with a warning in the output, not an error. This is fine — the warning message is informative. But it means agents relying on these flags for richer output will silently get less than expected without embeddings.

**7. Doctor's embeddings hint is info-level, not a warning.** `[i] embeddings-available: Embeddings database exists but embedding support not installed`. Since embeddings DB exists (it was built before), this feels like it should be `[!]` — you have a DB that the current install can't read. Info implies "here's something optional." Warning implies "here's something you had that's now partially broken." The degradation is graceful, but the signal level seems off.

**8. Persisted output for large conversations.** `siftd query 01KG8EWGAWDF -F` produced 75.5KB output and wrote it to a temp file, showing a 2KB preview. This is good — it prevented context overflow. The path was accessible and `cat` + `grep` worked fine. No friction here, just noting the behavior as expected.

**9. Pre-commit hook crashes on numpy import in no-embed install.** `./dev setup` (without `--embed`) installs the venv without numpy. When committing REPORT.md, the pre-commit hook ran `scripts/gen_docs.py`, which ultimately imports `siftd/storage/embeddings.py`, which does `import numpy as np` at module level. Result: `ModuleNotFoundError: No module named 'numpy'`. The commit still landed (soft failure — hook exit code ignored or hook failure is non-fatal), but the crash output is alarming for an agent that doesn't know the hook pattern. An agent seeing `ModuleNotFoundError` mid-commit might retry, diagnose a broken install, or halt. The fix (lazy import or `try/except ImportError`) is one line; the risk is that a no-embed dogfood run looks broken at commit time.
