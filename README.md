s
 t
  r
   a
    t
     a >_

Every conversation you have with an LLM coding tool — Claude Code, Gemini CLI, Codex, Aider — produces a log file. Decisions, rationale, dead ends, breakthroughs. When the session ends, that knowledge disappears into a directory you'll never open.

After a few months, you have thousands of conversations across dozens of projects. You've solved problems you'll face again. You've established patterns you'll forget you established. Your agents have researched concepts, proposed architectures, debugged issues — and none of that context carries forward to the next session.

The knowledge crosses boundaries. An insight from one project applies to another. A pattern that looks like a one-off is actually common across your work. A breakthrough happens mid-conversation about something else entirely — and six weeks later, you can't find it.

strata normalizes this data and makes it searchable.

- **Query** your sessions with composable filters or direct SQL. Drop `.sql` files in your config directory and they're available as subcommands — an agent builds a highly specific query for your domain, you ask it to encapsulate that into a `.sql`, and it's reusable from then on.
- **Search** by meaning. Embed your conversations, query them semantically with `ask`, then `tag` the results to thread further meaning through them. `philosophy:testing`, `genesis:strata`, `testing:anti-pattern`. When agents start making their own tags, it gets interesting — `forcing-function`, `the-great-deletion`, `self-similarity`.
- **Categorize** tool calls. Tools are normalized into broad strokes (`shell.execute`, `file.read`). Tags let you slice finer — `shell:vcs`, `shell:test` — for filtering and pattern discovery across your entire history.

strata was built with strata, subtask and Claude Code. I did not write any of the code in this project. I read (I won't claim all of it, but just about) the code and reviewed the output. The design decisions were mine. The documentation (when it has a voice) is mine. The development cycle was effectively a single ongoing conversation with a Claude Code instance, directing Subtasks to be split off for development, and manual compaction handled via a HANDOFF.md. Every attempt was made to smooth out friction, whether in the api surface or command construction. 

When strata was reliable enough to be used in conjunction, it was. Every feature came from review of its use by agents and myself. The intent behind the creation is to collapse cognitive loops by removing the friction inherent on both sides. Nothing provided by strata is unavailable otherwise - at the core, every one of these features can be handled by grepping jsonl files.

The value is collapsing the loop. Reduce the tool calls, reduce the time to response. Reduce the noise in the calls, reduce the context pollution. Reduce the context pollution, have richer, longer conversations in your window. Longer conversations in your window is more useful output you generate before having to compact. Compact into a single MD file that can be recursed through the project history with the _why_ provenance right there because the tool call to write it is right next to the reason it's being written - and now the loop has shrunk.

## Install

```bash
uv pip install .           # core functionality
uv pip install .[embed]    # with semantic search (strata ask)
```

Semantic search (`strata ask`) requires the `[embed]` extra. Core features — ingest, query, tags, peek — work without it.

## Getting started

strata reads the log files your tools already write. It doesn't record anything new — it just makes what's already there useful.

```bash
strata ingest
strata status
```

```
Conversations: 847
Prompts: 12,493
Responses: 14,271
Tool calls: 89,432
Workspaces: 31
```

Those 847 conversations span months of work across 31 projects. Every prompt you typed, every response you received, every file read and shell command run — structured and queryable.

## What accumulates

The data isn't just text. Each conversation captures:

- **What you asked and what you were told** — the full prompt+response exchange
- **What tools were used** — file reads, edits, shell commands, searches 
- **Where work happened** — which project directory, which model, when
- **How much it cost** — token counts, approximate pricing

This is your development history in structured form. Not commit messages after the fact — the actual working conversation as it happened - this is the sum of your cognitive effort - at the time it was done.

## Finding things

`strata query` browses conversations. `strata ask` searches them by meaning.

```bash
strata ask "how did I handle token refresh"
```

```
01JGK3M2P4Q5  0.847  [RESPONSE]  2025-01-15  myproject
  The token refresh uses a sliding window — store the refresh token in...

01JFXN2R1K4M  0.812  [PROMPT  ]  2024-10-03  auth-service
  Can you add automatic token refresh? The current flow requires...
```

That second result is from three months ago, in a different project. strata found it because the meaning matched, even though the words were different. The workspace filter (`-w`) narrows by project; `--thread` expands the top results into full conversation timelines for research.

Build the embeddings index first with `strata ask --index`. Everything runs locally — no API calls, no cloud.

## Your agents search too

This is where it gets interesting. strata ships a Claude Code plugin that gives agents access to your conversation history. When an agent needs context, it can search for it.

**Grounding in your practices.** You're starting tests in a new project. Instead of the agent guessing your preferences, it searches:

```bash
strata ask -w rill "testing philosophy"
strata ask -w experiments "prefer integration tests"
```

It finds 17 conversations across two projects where you discussed testing — your preference for integration tests over mocks, your fixture patterns, how you handle async. The agent writes tests grounded in how you actually work, not how it thinks you should.

**Finding the genesis of an idea.** A concept appears in your codebase but nobody remembers where it came from:

```bash
strata ask --first "event sourcing"
strata ask --chrono -w myproject "state management"
```

`--first` finds the earliest mention above a relevance threshold. `--chrono` traces how thinking evolved across sessions. You can reconstruct the intellectual history of a decision — when it was first proposed, how it changed, why alternatives were rejected.

**Mining tool use for automation.** Those 89k tool calls are structured data. Every shell command is categorized:

```bash
strata query --tool-tag shell:test      # which conversations ran tests?
strata query --tool-tag shell:vcs       # which ones touched git?
strata tools --by-workspace             # what patterns emerge per project?
```

When you see that `pytest tests/ -x -q` runs 200 times across your projects, that's a pattern worth encoding into a script, or adding to a hook. The tool data shows you what your development workflow actually looks like — not what you think it looks like. Pull patterns out of your entire development flow. Some are specific to a project, others might be universal. 

## The workflow teaches itself

strata's output is designed to guide the next step. Search results include tips:

```
Tip: tag useful results with `strata tag <id> research:auth`
     then retrieve later: `strata ask -l research:auth "query"`
```

The plugin's skill uses progressive disclosure — agents learn core search first, then output modes, then filtering, then the tagging workflow. Each level unlocks when the previous one is useful. An agent that finds something worth keeping learns to tag it. The next agent — in a different session, maybe a different project — can retrieve those tags.

```bash
strata tag 01JGK3 research:auth           # mark it
strata ask -l research:auth "token expiry" # find it later, from anywhere
```

## Building institutional memory

Tags are the synthesis layer. They're lightweight — no LLM calls, no schema changes — but they encode judgment. "This conversation matters. This is how we approach authentication. This is the decision we made about state management."

Over time, a curated layer builds on top of the raw data:

```bash
strata query -l research:          # everything tagged as research
strata ask --all-tags research:auth --all-tags review "token rotation"
```

This isn't just search anymore. It's institutional memory that persists across sessions, across projects, across agents. An agent working on project A can draw on decisions made in project B — not because someone documented it, but because the conversations were there and strata made them findable.

The cycle is: **conversations generate knowledge → strata makes it searchable → agents ground their work in what came before → those conversations generate more knowledge.**

Your agents carry semantic context through your entire development history.

## What else

**Live sessions** — inspect active sessions without waiting for ingestion:

```bash
strata peek
strata peek c520 --last 10
```

**Health checks** — `strata doctor`

**Raw SQL** — `strata query sql cost --var limit=20`

**Library API** — `from strata import list_conversations, hybrid_search`

**Custom adapters** — `~/.config/strata/adapters/my_tool.py`

## Going deeper

| Topic | Doc |
|-------|-----|
| All CLI commands and flags | [docs/cli.md](docs/cli.md) |
| Configuration | [docs/config.md](docs/config.md) |
| Search pipeline, diversity tuning, benchmarking | [docs/search.md](docs/search.md) |
| Tag system and conventions | [docs/tags.md](docs/tags.md) |
| Library API reference | [docs/api.md](docs/api.md) |
| Data model and schema | [docs/data-model.md](docs/data-model.md) |
| Writing custom adapters | [docs/adapters.md](docs/adapters.md) |
| SQL queries and examples | [docs/queries.md](docs/queries.md) |
| Claude Code plugin | [docs/plugin.md](docs/plugin.md) |
