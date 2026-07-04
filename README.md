# siftd

You've been using Claude Code, Aider, Gemini CLI, or Codex for months. Each session produces a log file — decisions made, problems solved, dead ends explored. When the session ends, that knowledge sits in a directory you'll never open.

siftd makes it searchable.

## Install

```bash
pip install siftd
```

## You have sessions everywhere

Run your first ingest to see what's already there:

```bash
siftd ingest
```

```
==================================================
SUMMARY
==================================================
Files found:    523
Files ingested: 448
Files replaced: 0
Files skipped:  75

Conversations: 448
Prompts:       6,241
Responses:     7,893
Tool calls:    52,107

--- By Harness ---

claude_code:
  conversations: 312
  prompts: 4,102
  responses: 5,210
  tool_calls: 41,893

aider:
  conversations: 89
  prompts: 1,456
  responses: 1,834
  tool_calls: 7,241

gemini_cli:
  conversations: 47
  prompts: 683
  responses: 849
  tool_calls: 2,973
```

siftd found 448 conversations you've had over the past few months. Each one captured prompts, responses, tool calls, file edits, shell commands — structured and queryable.

See what accumulated:

```bash
siftd db stats
```

```
Database: /home/you/.local/share/siftd/siftd.db
Size: 42380.2 KB

--- Counts ---
  Conversations: 448
  Prompts: 6,241
  Responses: 7,893
  Tool calls: 52,107
  Harnesses: 3
  Workspaces: 23
  Tools: 18
  Models: 5
  Ingested files: 448

--- Workspaces (top 10) ---
  myproject: 89 conversations (last 2025-01-15 14:32)
  auth-service: 45 conversations (last 2025-01-14 16:45)
  ...
```

Browse recent work:

```bash
siftd query
```

```
01JGK3M2P4Q5  2025-01-15 14:32  myproject      claude-opus-4-5   12p/34r  18.2k tok  $0.2847
01JGK2N1R3S4  2025-01-15 10:17  auth-service   claude-opus-4-5   8p/21r   12.5k tok  $0.1923
01JGK1P0Q2R3  2025-01-14 16:45  myproject      claude-sonnet-4   5p/12r   6.3k tok   $0.0412
...
```

Each row is a conversation. The ID prefix is enough to reference it — `01JGK3` will match `01JGK3M2P4Q5`.

Look at a specific conversation:

```bash
siftd query 01JGK3
```

This shows the full exchange: every prompt you typed, every response, every tool call with its inputs and outputs.

## You remember working on something

A week ago you solved a tricky auth problem. You don't remember which project or what you called it. You just remember the shape of the problem.

Search for it:

```bash
siftd search "token refresh"
```

```
01JGK3M2P4Q5  2025-01-15 14:32  myproject        claude-opus-4-5   12p/34r
01JFXN2R1K4M  2024-12-03 09:15  auth-service     claude-opus-4-5   8p/19r
```

Found two conversations mentioning "token refresh". Without embeddings installed, this uses keyword matching (FTS5). But maybe you used different words — "session expiry", "credential renewal". Keyword search won't find those.

Install the embedding extra to upgrade `siftd search` to hybrid mode — same command, better results:

```bash
pip install siftd[embed]
siftd embed    # build embeddings (runs locally, no API calls)
```

Now the same command finds by meaning:

```bash
siftd search "handling expired credentials"
```

```
Results for: handling expired credentials

  01JGK3M2P4Q5  0.847  [RESPONSE]  2025-01-15  myproject
    The token refresh uses a sliding window approach — store the refresh token in httpOnly cookie, check expiry on each request...

  01JFXN2R1K4M  0.812  [RESPONSE]  2024-12-03  auth-service
    For credential renewal, we went with a background refresh 30 seconds before expiry rather than waiting for a 401...
```

The second result is from a different project, using different words, but siftd found it because the meaning matched.

Narrow results by workspace or time:

```bash
siftd search -w myproject "auth"           # only myproject
siftd search --since 2025-01-01 "testing"  # recent conversations
siftd search -n 20 "error handling"        # more results
```

See the surrounding context:

```bash
siftd search --around "token refresh" --turns -2:+2  # window around the match
siftd search --mode thread "architecture"            # expand top hits into full threads
```

## This is useful — you'll need it again

You found the auth conversation. It's exactly the pattern you need. Tag it so you can find it instantly next time:

```bash
siftd tag 01JGK3 decision:auth
```

Tags are freeform. Use prefixes to create namespaces:

```bash
siftd tag 01JGK3 decision:auth      # architectural decisions
siftd tag 01JFXN research:oauth     # research/exploration
siftd tag 01JGK1 pattern:testing    # reusable patterns
```

Retrieve tagged conversations:

```bash
siftd query -l decision:auth              # exact tag
siftd query -l decision:                  # all decision:* tags
siftd search -l research: "authentication" # search within tagged
```

List your tags:

```bash
siftd tag list
```

```
  decision:auth (3 conversations)
  decision:caching (2 conversations)
  pattern:testing (5 conversations)
  research:oauth (1 conversations)
  shell:test (847 tool_calls)
  shell:vcs (312 tool_calls)
```

Tag the most recent conversation without looking up the ID:

```bash
siftd tag -n 1 decision:deployment
```

## You want to see a session in progress

Ingest runs periodically, but sometimes you want to see what's happening right now. `peek` reads log files directly:

```bash
siftd peek
```

```
  c520f862  myproject        just now      12 exchanges     claude-opus-4-5 claude_code
  a3d91bc7  auth-service     2h ago        8 exchanges      claude-opus-4-5 claude_code
```

Look at the last few exchanges in a session:

```bash
siftd peek c520           # last 5 exchanges
siftd peek c520 -n 10     # last 10 exchanges
siftd peek c520 --full    # no truncation
```

This is useful for checking on long-running agent sessions or reviewing work before it's ingested.

## You need to reference this in a PR

You're opening a pull request and want to include the conversation that led to this implementation. Export it:

```bash
siftd export 01JGK3
```

```markdown
## Session 01JGK3M2P4
*myproject · 2025-01-15 14:32*

1. Can you help me implement token refresh? The current flow requires...

2. What about handling the race condition when multiple tabs...

3. Let's add tests for the refresh logic...
```

Export to a file:

```bash
siftd export 01JGK3 -o context.md
```

Export your most recent session:

```bash
siftd export -n 1
```

Export multiple sessions or filter by tag:

```bash
siftd export -n 3                         # last 3 sessions
siftd export -l decision:auth             # all auth decisions
siftd export -w myproject --since 7d      # recent work in a project
```

## You use a tool siftd doesn't support

siftd ships adapters for Claude Code, Aider, Gemini CLI, and Codex. If you use something else, write an adapter.

Start from the template or copy an existing adapter to modify:

```bash
siftd copy adapter template       # blank template
siftd copy adapter claude_code    # copy a built-in to customize
siftd copy adapter --all          # copy all built-ins
# Creates files in ~/.config/siftd/adapters/
```

Edit the adapter to parse your tool's log format. An adapter needs three things:

1. `NAME` — identifier for the adapter
2. `DEFAULT_LOCATIONS` — where to find log files
3. `parse(path)` — return a `Conversation` from a log file

Verify it works:

```bash
siftd adapters          # should list your adapter
siftd ingest -v         # verbose output shows what's parsed
siftd doctor            # run health checks
```

See [Writing Adapters](docs/guides/writing-adapters.md) for the full guide.

## Commands

| Command | Purpose |
|---------|---------|
| `ingest` | Import conversation logs from all adapters |
| `query` | List conversations, filter by workspace/date/tag, view details |
| `search` | Semantic search (requires `[embed]` extra) |
| `tag` | Apply tags to conversations; `tag list` to browse |
| `export` | Export conversations for PR review or context |
| `peek` | View live sessions without waiting for ingest |
| `db` | Database operations — `stats`, `info`, `backup`, `restore`, `vacuum`, `slice`, `path` |
| `tools` | Shell command category summary and tool usage patterns |
| `doctor` | Health checks and maintenance |
| `adapters` | List discovered adapters |
| `config` | View and modify configuration |
| `install` | Install optional extras (e.g., `siftd install embed`) |

Run `siftd <command> --help` for full options.

## Going deeper

To understand how siftd works under the hood:

- [Documentation](docs/index.md) — concepts, guides, and reference

## License

MIT
