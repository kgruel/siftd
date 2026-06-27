# Using siftd with agents

An agent's memory ends when its context window does. Compact the session, start a new one, hand off to a sub-agent, and the reasoning that got you here is gone. siftd is a two-way memory that outlives the session: agents **record** into it automatically and **recall** from it on demand.

Two halves:

- **Record** — Claude Code lifecycle hooks register the live session and re-ingest it as you work. No manual `siftd ingest` step.
- **Recall** — the `/siftd` skill teaches the agent a small vocabulary for searching, browsing, and tagging that history.

On Claude Code, one command wires both:

```bash
siftd install plugin
```

The plugin bundles the skill (recall) and the hooks (record). On every other harness, install just the recall skill — see [Recall on demand](#recall-on-demand).

## Record automatically

The goal is that you never think about ingestion. You work; the session keeps landing in the database as you go — re-ingested after each turn, tags and all.

`siftd install plugin` copies the bundled plugin to `~/.claude/plugins/siftd` (or `.claude/plugins/siftd` with `--scope project`) and wires four Claude Code lifecycle hooks. Two are load-bearing for memory:

| Hook | Fires | What it runs |
|------|-------|--------------|
| `SessionStart` | start / resume / compact | `siftd register --session claude_code::<id> --adapter claude_code --workspace <cwd>` — registers the live session so `siftd tag --current` can resolve it |
| `Stop` | end of each response turn | `siftd ingest -a claude_code -q` — re-ingests this session's transcript and applies any tags queued during it |

The other two hooks are nudges, not bookkeeping: `UserPromptSubmit` suggests loading the skill when you mention past work, and `PostToolUse` prints a contextual tip after a `siftd` command. Neither touches the database.

The effect is that the `Stop` hook ingests only the `claude_code` adapter (~0.7s, not a full ingest) at each turn boundary, so the transcript is current within moments of you stepping away. If a very large `~/.claude/projects` pushes that past the 10s hook timeout, the tags stay queued in `pending_tags` and apply on the next ingest that completes — nothing is lost.

### Sub-agents nest under their parent

When Claude Code spawns a sub-agent (the `Task` tool), the sub-agent runs in its own transcript. siftd keeps the relationship explicit in the `external_id`:

```
claude_code::01JGK3M2P4                    # parent session
claude_code::01JGK3M2P4::agent::a7f3c1     # sub-agent, nested under it
```

The child's id is the parent's id plus `::agent::<agentId>`. Because the parent id is a literal prefix of the child id, the lineage survives ingest — each conversation is deduped independently by `external_id`, but you can always see which session spawned which.

That prefix also makes live tagging robust across the boundary. If you tag during a sub-agent run, the tag is queued against the parent session id. On ingest the sub-agent conversation consumes pending tags for its own id first, then falls back to the parent id (split on `::agent::`) — so a "tag this session" gesture lands on exactly one conversation in the session, whichever is ingested first. Claude Code's `agent-<id>.meta.json` sidecar also binds the sub-agent's type and description, captured as the `subagent_type` and `agent_description` conversation attributes.

### Other harnesses

Only Claude Code has the lifecycle hooks. Other tools (Codex, Gemini CLI, Aider, …) record into siftd the way they always have: their adapters discover the logs on `siftd ingest`. Run ingest by hand, or on a schedule — there's no live session to register, so tags applied during those sessions use `--last` rather than `--current`.

## Recall on demand

Recording fills the database; the `/siftd` skill teaches an agent to read from it. Install it for whichever harness you're driving:

```bash
siftd install skill                       # Claude Code (default)
siftd install skill --harness codex_cli   # Codex CLI
siftd install skill --harness pi_agent    # Pi Agent
```

What lands where depends on the harness. Claude Code and Pi get the structured skill — a `SKILL.md` plus a `reference/` directory the agent reads on demand. The rest get a single rendered markdown instructions file any LLM can follow:

| `--harness` | Format | Lands at |
|-------------|--------|----------|
| `claude_code` (default) | skill | `~/.claude/skills/siftd/` (SKILL.md + reference/) — `install plugin` instead lands the same skill, plus hooks and commands, under `~/.claude/plugins/siftd/` |
| `pi_agent` | skill | `~/.pi/agent/skills/siftd/` |
| `codex_cli` | instructions | `~/.codex/siftd.md` |
| `gemini_cli` | instructions | `~/.gemini/siftd.md` |
| `copilot_cli` | instructions | `.github/siftd-instructions.md` (project) |
| `aider` | instructions | `./.aider.siftd.md` (project) |

`--scope user|project` selects between a home-directory install and a project-local one. Claude Code supports both (`~/.claude/skills/siftd` vs `.claude/skills/siftd`); the rest have a single fixed scope and use it regardless of the flag — Copilot and Aider are project-scoped by nature, the others user-scoped.

On Claude Code, prefer `install plugin` over `install skill`: the plugin bundles the same skill **and** the recording hooks. `install skill` is the right call for every other harness, which records via `siftd ingest` and only needs the recall vocabulary. The plugin already includes the skill, so install one or the other — `install plugin` will clean up a standalone skill it finds shadowing it.

## The /siftd vocabulary

The skill hands the agent a compact vocabulary. Each `/siftd` form maps to a real siftd command — the agent runs the command, then summarizes the result in natural language rather than dumping raw output:

| You type | Agent runs |
|----------|-----------|
| `/siftd "query"` | `siftd search "query" --view=thread` |
| `/siftd -w proj "query"` | `siftd search -w proj "query" --view=thread` |
| `/siftd --recent` | `siftd query -n 5` |
| `/siftd --genesis "concept"` | `siftd search --select=first --sort=time "concept"` |
| `/siftd:tag <tags>` | `siftd tag --current <tags>` |

`--view=thread` returns a narrative drill-down rather than raw chunks — the right shape for an agent that's going to read and summarize. `--genesis` traces where an idea started: `--select=first` picks the chronologically earliest match above threshold, and `--sort=time` orders by when, not by relevance.

When you want to drive the workflow yourself — raw output, no agent interpretation — the plugin also ships direct-execution commands:

| Command | Runs |
|---------|------|
| `/siftd:search "query"` | search, raw output |
| `/siftd:query [id]` | list conversations, or drill into one |
| `/siftd:peek [id]` | live/recent sessions, bypassing the database |
| `/siftd:tag <tags>` | `siftd tag --current` with session-detection feedback |

## Context injection

Recall pulls history into the agent's reasoning; sometimes you want history in the prompt itself. `siftd export` renders a conversation to markdown you — or an agent — can paste:

```bash
siftd export --last --thinking --tools
```

`--last` takes the most recent session (`--last N` for the last N), `--thinking` includes the model's reasoning blocks, and `--tools` expands tool inputs and results. Output goes to stdout as markdown, or to a file with `-o`:

```bash
siftd export --last --full -o context.md   # --full = thinking + tools
```

Pipe it straight into a fresh session to carry context across a compaction or a handoff to a new agent:

```bash
siftd export --last --full | pbcopy        # then paste into the new prompt
```

## Tagging conventions for agent-built memory

A search index is recall by content; tags are recall by intent. When an agent (or you) decides a conversation matters, tag it — that's how the database becomes institutional memory instead of an undifferentiated pile of transcripts.

The conventions are shared between the skill and the project's `CLAUDE.md`, so every agent and session reaches for the same namespaces:

| Prefix | What it marks |
|--------|---------------|
| `decision:*` | Architectural / design decisions |
| `research:*` | Investigation findings worth keeping |
| `useful:*` | Reusable patterns and examples |
| `rationale:*` | Why X over Y |
| `genesis:*` | First discussion of a concept |

During a live Claude Code session, tag with `--current` — the `SessionStart` hook already registered the session, so siftd resolves which conversation you mean and queues the tag as pending. The `Stop` hook applies it on ingest:

```bash
siftd tag --current decision:auth          # queued now, applied on the next ingest
```

If no live session is detected, `--current` falls back to `--last 1` (the most recent ingested conversation), and the command output tells you which path it took.

Retrieval closes the loop. A trailing colon turns `-l` into a prefix match, so you can pull a single tag or a whole namespace:

```bash
siftd query -l decision:auth               # one decision
siftd query -l decision:                   # every decision (prefix match)
siftd search -l research: "token rotation" # search, scoped to a namespace
```

The pattern: an agent researches, tags what it finds with `research:*`, and a later session retrieves it with `siftd query -l research:<topic>` — no re-derivation, no re-search. Memory that one agent builds, the next one reads.

## See also

- [Tags](../concepts/tags.md) — naming conventions, boolean filtering (`-l` / `--all-tags` / `--no-tag`), and building institutional memory
- [Search](../concepts/search.md) — FTS5 vs embeddings, how hybrid search combines them, tuning diversity and recency
- [Delegation contract](delegation-contract.md) — how a command's local and served forms stay in sync
