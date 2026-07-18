# CLI Reference

_Auto-generated from `--help` output._

## siftd

```
siftd 0.12.0 - Aggregate and query LLM conversation logs

usage: siftd [-h] [--version] [--db PATH] <command> ...

lanes:
  EXPLORE   query · search · show · report · peek
  CURATE    tag · export
  INGEST    ingest · adapters
  MAINTAIN  doctor · db · embed
  SHARE     serve · auth
  SETUP     install · config

Run 'siftd <command> --help' for details.
Advanced (hidden): backfill, copy, id, migrate, register, session-id, upgrade
```

## siftd query

```
siftd > query
List and filter conversations by metadata

usage: siftd query [-h] [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE]
                   [-l NAME] [--all-tags NAME] [--no-tag NAME] [--on KIND]
                   [-t NAME] [--tool-tag NAME] [--owner USER] [--json]
                   [-n LIMIT] [--no-hints] [-F] [-b] [--chars N] [--thinking]
                   [--tools [FILTER]] [--tool-chars N] [-v] [--oldest] [--stats]

FILTERS
  -w, --workspace SUBSTR  Filter by workspace path substring
  -m, --model NAME        Filter by model name
  --since DATE            Conversations after this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  --before DATE           Conversations before this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  -l, --tag NAME          Filter by tag (repeatable, OR logic)
  --all-tags NAME         Require all specified tags (AND logic)
  --no-tag NAME           Exclude conversations with this tag (NOT logic)
  --on KIND               Scope tag filters to a specific target kind
                          (repeatable). Default: match tags on any kind
                          (conversation, prompt, response, tool_call, exchange).
  -t, --tool NAME         Filter by canonical tool name (e.g. shell.execute)
  --tool-tag NAME         Filter by tool call tag (e.g. shell:test)
  --owner USER            Filter to conversations owned by this user

OUTPUT
  --json             Output as JSON
  -n, --limit LIMIT  Number of results to show
  --no-hints         Suppress hint-severity caveat findings.
  -v, --verbose      Full table with all columns
  --oldest           Sort by oldest first (default: newest first)
  --stats            Show summary totals after list

VIEW
  -F, --full        Full text (no truncation)
  -b, --brief       Compact view (80 char truncation)
  --chars N         Truncate text at N characters
  --thinking        Show model thinking/reasoning blocks
  --tools [FILTER]  Show tool inputs/results (optional filter: tool name prefix
                    or 'errors')
  --tool-chars N    Truncate tool input/result at N characters (default: 120)

IDs shown are 12-char prefixes; any unambiguous prefix works. To read one
conversation use 'siftd show <id>'; content search is 'siftd search'.

examples:
  siftd query                          # recent conversations
  siftd query -n 20                    # the last 20
  siftd query -w myproject             # filter by workspace
  siftd query --since 7d               # started in the last 7 days
  siftd query -l research:auth         # tagged research:auth
  siftd show <id>                      # read one conversation
```

## siftd search

```
siftd > search
Search conversations (auto-selects FTS5 or semantic based on embed.backend)

usage: siftd search [-h] [--history [N]] [-w SUBSTR] [-m NAME] [--since DATE]
                    [--before DATE] [-l NAME] [--all-tags NAME] [--no-tag NAME]
                    [--on KIND] [-t NAME] [--tool-tag NAME] [--owner USER]
                    [--json] [-n LIMIT] [-F] [-v] [--format NAME]
                    [--around PHRASE] [--turns A:B] [--select SELECTOR]
                    [--sort ORDER] [--view VIEW] [--refs [FILES]]
                    [--mode ENGINE] [--recall N] [--threshold SCORE] [--raw-fts]
                    [--no-diversity] [--lambda FLOAT] [--recency]
                    [--recency-half-life DAYS] [--recency-max-boost MULT]
                    [--no-exclude-active] [--include-derivative] [query ...]

ARGUMENTS
  query  Natural language search query

OPTIONS
  --history [N]  List the last N captured searches (default: 20) instead of
                 searching. Local-only; see search.log config.

FILTERS
  -w, --workspace SUBSTR  Filter by workspace path substring
  -m, --model NAME        Filter by model name
  --since DATE            Conversations after this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  --before DATE           Conversations before this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  -l, --tag NAME          Filter by tag (repeatable, OR logic)
  --all-tags NAME         Require all specified tags (AND logic)
  --no-tag NAME           Exclude conversations with this tag (NOT logic)
  --on KIND               Scope tag filters to a specific target kind
                          (repeatable). Default: match tags on any kind
                          (conversation, prompt, response, tool_call, exchange).
  -t, --tool NAME         Filter by canonical tool name (e.g. shell.execute)
  --tool-tag NAME         Filter by tool call tag (e.g. shell:test)
  --owner USER            Filter to conversations owned by this user

OUTPUT
  --json             Output as JSON
  -n, --limit LIMIT  Number of results to show (default: 10)
  -v, --verbose      Show full chunk text
  --format NAME      Use named formatter (built-in or drop-in plugin)

VIEW
  -F, --full         Full text (no truncation)
  --around PHRASE    Anchor at the first FTS5 phrase match in the conversation
  --turns A:B        Turn range relative to anchor, e.g. -2:+2 or 5:10 (requires
                     an anchor flag)
  --select SELECTOR  Result selector: all (default) or first (chronologically
                     earliest match above threshold)
  --sort ORDER       Sort order: score (default, relevance) or time
                     (chronological). Incompatible with
                     --mode=thread/conversations.
  --view VIEW        Result shape: chunks (default), thread (narrative
                     drill-down), or conversations (aggregated ranking)
  --refs [FILES]     Show file references; optionally filter by comma-separated
                     basenames

SEARCH
  --mode ENGINE      Search engine: auto (default), fts, semantic, or hybrid.
                     auto picks hybrid when embeddings are installed, else fts.
  --recall N         FTS5 conversation recall limit (default: per-embedder)
  --threshold SCORE  Filter results below this score (e.g., 0.7)
  --raw-fts          Pass query directly to FTS5 without tokenization (advanced:
                     skips OR fallback)

RANKING
  --no-diversity            Disable MMR reranking for deterministic pure
                            relevance order
  --lambda FLOAT            MMR lambda: 1.0=relevance, 0.0=diversity (default:
                            0.7)
  --recency                 Boost recent results (exponential decay, mild 15%
                            boost)
  --recency-half-life DAYS  Days until recency boost decays to half (default:
                            30)
  --recency-max-boost MULT  Max boost multiplier for today's results (default:
                            1.15)
  --no-exclude-active       Include results from active sessions (excluded by
                            default)
  --include-derivative      Include derivative conversations (siftd search/query
                            results)

Auto-selects the engine: hybrid (FTS5 + semantic) when an embedding
backend is configured, else FTS5 keyword search. Set embed.backend for a remote
provider, or run 'siftd install embed' for the local backend.

examples:
  siftd search "error handling"                   # auto (hybrid or FTS5)
  siftd search -w myproject "auth flow"           # filter by workspace
  siftd search --since 7d "testing"               # filter by date
  siftd search "design decision" --view thread    # narrative: top conversations
  siftd search "why X" --around why --turns -2:+2 # window around a phrase match
  siftd search -l research: "auth flow"           # search within tagged conversations

(--context was removed; use --around PHRASE --turns -N:+N)
```

## siftd show

```
siftd > show
Read one conversation (or event) in detail

usage: siftd show [-h] [--json] [-F] [-b] [--chars N] [--thinking]
                  [--tools [FILTER]] [--tool-chars N]
                  [--from-start | --from-end | --at-turn N | --around PHRASE]
                  [--exchanges N | --turns A:B] [--summary] [--neighbors]
                  conversation_id

ARGUMENTS
  conversation_id  Conversation or event ID (any unambiguous prefix)

OUTPUT
  --json  Output as JSON

VIEW
  -F, --full        Full text (no truncation)
  -b, --brief       Compact view (80 char truncation)
  --chars N         Truncate text at N characters
  --thinking        Show model thinking/reasoning blocks
  --tools [FILTER]  Show tool inputs/results (optional filter: tool name prefix
                    or 'errors')
  --tool-chars N    Truncate tool input/result at N characters (default: 120)
  --from-start      Anchor at the start of the conversation (turn 0)
  --from-end        Anchor at the end of the conversation (last turn)
  --at-turn N       Anchor at the N-th turn (0-indexed)
  --around PHRASE   Anchor at the first FTS5 phrase match in the conversation
  --exchanges N     Number of turns to show from anchor (requires an anchor
                    flag)
  --turns A:B       Turn range relative to anchor, e.g. -2:+2 or 5:10 (requires
                    an anchor flag)
  --summary         Summary only (metadata, no turns)
  --neighbors       Include prev_event_id/next_event_id in event detail output

IDs may be any unambiguous prefix. Anchors (--from-start / --from-end /
--at-turn / --around) pick a position; --exchanges / --turns set the window around it.
No anchor shows the whole conversation.

examples:
  siftd show 01HX4G7K                            # full conversation
  siftd show 01HX4G7K --summary                  # metadata only, no turns
  siftd show 01HX4G7K --from-end --exchanges 5   # last 5 turns
  siftd show 01HX4G7K --at-turn 4 --turns -1:+2  # turns 3-6, around turn 4
  siftd show 01HX4G7K --around error --turns -2:+2  # window around a phrase
```

## siftd report

```
siftd > report
Run saved SQL reports (parameterized .sql queries)

usage: siftd report [-h] [--var KEY=VALUE] [name]

ARGUMENTS
  name  Report name (omit to list available reports)

OPTIONS
  --var KEY=VALUE  Substitute $KEY with VALUE in the report SQL

Run saved SQL reports. Built-in reports work out of the box;
.sql files in ~/.config/siftd/queries/ add your own or override a built-in
(same filename wins).

A report is a named .sql file with optional $KEY placeholders. Run without a
name to list available reports. To customize a built-in, copy it first:
  siftd copy query cost            # copy the 'cost' report to your queries dir

examples:
  siftd report                          # list available reports
  siftd report cost                     # run the 'cost' report
  siftd report cost --var ws=proj       # run with variable substitution
```

## siftd peek

```
siftd > peek
Inspect live sessions from disk (bypasses SQLite)

usage: siftd peek [-h] [-w SUBSTR] [--branch SUBSTR] [--all] [--main-only]
                  [--children ID] [--json] [-n LIMIT] [-F] [-b] [--chars N]
                  [--thinking] [--exchanges N] [--tools] [-f]
                  [--timeout SECONDS] [--tail] [--tail-lines N]
                  [--last-response] [--last-prompt] [session_id]

ARGUMENTS
  session_id  Session ID prefix for detail view

FILTERS
  -w, --workspace SUBSTR  Filter by workspace name substring
  --branch SUBSTR         Filter by worktree branch substring
  --all                   Include inactive sessions (not just last 2 hours)
  --main-only             Only show main sessions (exclude subagents)
  --children ID           Show only children of the specified parent session

OUTPUT
  --json             Output as JSON
  -n, --limit LIMIT  Number of results to show

VIEW
  -F, --full         Full text (no truncation)
  -b, --brief        Compact view (80 char truncation)
  --chars N          Truncate text at N characters
  --thinking         Show model thinking/reasoning blocks
  --exchanges N      Detail mode: number of exchanges to show (default: 5)
  --tools            Show tool inputs/results inline when available
  -f, --follow       Follow a live session in real time (like tail -f)
  --timeout SECONDS  Exit after SECONDS of wall-clock time (for use with
                     --follow)
  --tail             Raw JSONL tail (last 20 records)
  --tail-lines N     Number of records for --tail (default: 20)
  --last-response    Output only the last assistant response (raw text, no
                     formatting)
  --last-prompt      Output only the last user prompt (raw text, no formatting)

examples:
  siftd peek                     # latest sessions (active, last 2h)
  siftd peek --all -n 50         # all sessions, first 50
  siftd peek -w myproject        # filter by workspace name
  siftd peek c520f862            # detail view (last 5 exchanges)
  siftd peek c520 --follow       # follow a live session in real time
  siftd peek --last-response     # output the last assistant response (raw)

NOTE: Session content may contain sensitive information (API keys, credentials, etc.).
```

## siftd tag

```
siftd > tag
Apply, remove, list, rename, or delete tags.

usage: siftd tag [-h] [-n [N]] [-r] [--session ID] [--current]
                 [--exchange INDEX | --last-prompt | --last-response |
                 --last-exchange | --last-tool-call] [--prefix PREFIX]
                 [--limit LIMIT] [--force] [--by-workspace] [--json] [-w SUBSTR]
                 [-m NAME] [--since DATE] [--before DATE] [-l NAME]
                 [--all-tags NAME] [--no-tag NAME] [--on KIND] [--owner USER]
                 [positional ...]

ARGUMENTS
  positional  [entity_type] entity_id tag [tag2 ...] | apply | remove | list |
              rename | delete

OPTIONS
  -n, --last, --latest [N]  Tag N most recent conversations (default: 1 if flag
                            used without N)
  -r, --remove              Remove tag instead of applying
  --session ID              Queue tag for a live session (applied at ingest)
  --current                 Auto-detect current session (falls back to --last)
  --exchange INDEX          Tag specific exchange (1-based, requires --session)
  --last-prompt             Tag the last prompt of the session (requires
                            --session/--current)
  --last-response           Tag the last response of the session (requires
                            --session/--current)
  --last-exchange           Tag the last exchange of the session (requires
                            --session/--current)
  --last-tool-call          Tag the last tool_call of the session (requires
                            --session/--current)
  --prefix PREFIX           Filter tag list by prefix (use with 'tag list')
  --limit LIMIT             Result cap (drill-down: default 10, --by-workspace:
                            default 20 workspaces)
  --force                   Force delete even if tag has associations (use with
                            'tag delete')
  --by-workspace            Group tag counts by workspace (use with 'tag list').
                            Counts only event-backed tags (tool_call, prompt,
                            response, exchange); conversation-level tags are
                            excluded. Composes with --on, --prefix, -w, --owner,
                            --all-tags, --limit only.
  --json                    Output as JSON (use with 'tag list --by-workspace')

FILTERS
  -w, --workspace SUBSTR  Filter by workspace path substring
  -m, --model NAME        Filter by model name
  --since DATE            Conversations after this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  --before DATE           Conversations before this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  -l, --tag NAME          Filter by tag (repeatable, OR logic)
  --all-tags NAME         Require all specified tags (AND logic)
  --no-tag NAME           Exclude conversations with this tag (NOT logic)
  --on KIND               Scope tag filters to a specific target kind
                          (repeatable). Default: match tags on any kind
                          (conversation, prompt, response, tool_call, exchange).
  --owner USER            Filter to conversations owned by this user

examples:
  siftd tag 01HX... important              # tag conversation (default)
  siftd tag 01HX... important review       # apply multiple tags at once
  siftd tag --last important               # tag most recent conversation
  siftd tag --last important review        # multiple tags on most recent
  siftd tag --last 3 review                # tag 3 most recent conversations
  siftd tag workspace 01HY... proj         # explicit entity type
  siftd tag tool_call 01HZ... slow         # tag a tool call
  siftd tag --remove 01HX... important     # remove tag from conversation
  siftd tag --remove --last 1 important    # remove from most recent
  siftd tag -r workspace 01HY... proj      # remove from workspace

subcommands:
  siftd tag apply 01HX... important         # explicit apply (same as positional)
  siftd tag apply --last important          # apply to most recent via subcommand
  siftd tag remove 01HX... important        # explicit remove (same as --remove)
  siftd tag remove --last important         # remove from most recent via subcommand
  siftd tag list                            # list all tags
  siftd tag list --prefix research:         # filter by prefix
  siftd tag list research:auth              # show conversations with tag
  siftd tag rename old-name new-name        # rename a tag
  siftd tag delete old-tag                  # delete (refuses if applied)
  siftd tag delete old-tag --force          # delete with associations

live session tagging:
  siftd tag --current decision:auth              # auto-detect session, queue tag
  siftd tag --session abc123 decision:auth       # queue tag for session
  siftd tag --session abc123 --exchange 5 key    # queue tag for exchange 5
```

## siftd export

```
siftd > export
Export conversations as markdown or JSON

usage: siftd export [-h] [-n [N]] [-w SUBSTR] [--since DATE] [--before DATE]
                    [-l NAME] [--no-tag NAME] [--on KIND] [-s QUERY]
                    [--owner USER] [--json] [-F] [-b] [--thinking] [--tools]
                    [--no-header] [--view {conversations,elements}] [-o FILE]
                    [conversation_id]

ARGUMENTS
  conversation_id  Conversation ID (prefix match)

OPTIONS
  -n, --last, --latest [N]  Export N most recent sessions (default: 1 if no ID
                            given)

FILTERS
  -w, --workspace SUBSTR  Filter by workspace path substring
  --since DATE            Conversations after this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  --before DATE           Conversations before this date (YYYY-MM-DD, 7d, 1w,
                          yesterday, today)
  -l, --tag NAME          Filter by tag (repeatable, OR logic)
  --no-tag NAME           Exclude conversations with this tag (NOT logic)
  --on KIND               Scope tag filters to a specific target kind
                          (repeatable). Default: match tags on any kind
                          (conversation, prompt, response, tool_call, exchange).
  -s, --search QUERY      Full-text search filter
  --owner USER            Filter to conversations owned by this user

OUTPUT
  --json  Output as JSON

VIEW
  -F, --full   Full text (no truncation)
  -b, --brief  Compact view (80 char truncation)
  --thinking   Show model thinking/reasoning blocks

EXPORT OPTIONS
  --tools                          Expand tool inputs and results (default:
                                   summary)
  --no-header                      Omit session metadata header
  --view {conversations,elements}  What to export: whole conversations (default)
                                   or the tagged elements (requires --tag)
  -o, --output FILE                Write to file instead of stdout

examples:
  siftd export --last                   # export most recent session
  siftd export --last 3                 # export last 3 sessions
  siftd export 01HX4G7K                 # export specific session (prefix match)
  siftd export --last --thinking        # include thinking blocks
  siftd export --last --tools           # include tool inputs/results
  siftd export --last --full            # everything: thinking + tools
  siftd export --last --brief           # condensed output
  siftd export --last --json            # structured JSON output
  siftd export --last -o context.md     # write to file
```

## siftd ingest

```
siftd > ingest
Ingest logs from all sources

usage: siftd ingest [-h] [-q | -v] [-p DIR] [-a NAME] [--json] [--rebuild-fts]

OPTIONS
  -q, --quiet         Only show totals line
  -v, --verbose       Show per-adapter skip breakdowns
  -p, --path DIR      Additional directories to scan (can be repeated)
  -a, --adapter NAME  Only run specific adapter(s) (can be repeated)
  --json              Output newline-delimited JSON events
  --rebuild-fts       Rebuild FTS index from existing data (skips ingestion)

examples:
  siftd ingest                      # ingest from all adapters
  siftd ingest -q                   # quiet: totals line only
  siftd ingest -v                   # verbose: per-adapter skip breakdowns
  siftd ingest -a claude_code       # only run claude_code adapter
  siftd ingest -p ~/logs -p /tmp    # scan additional directories
  siftd ingest --rebuild-fts        # rebuild FTS index from scratch
```

## siftd adapters

```
siftd > adapters
List discovered adapters

usage: siftd adapters [-h] [--json]

OPTIONS
  --json  Output as JSON
```

## siftd doctor

```
siftd > doctor
Run health checks and maintenance

usage: siftd doctor [-h] [--json] [--strict] [--pending-tags] [--deep] [--fast]
                    [--blob-refcount] [--triggers] [--no-hints] [subcommand ...]

ARGUMENTS
  subcommand  list | run [checks...] | fix | <check-name>

OPTIONS
  --json           Output as JSON
  --strict         Exit 1 on warnings (not just errors). Useful for CI.
  --pending-tags   Clean up stale sessions and orphaned pending tags (use with
                   'fix')
  --deep           Include deep integrity checks (slower).
  --fast           Run only fast checks (skips slow and deep).
  --blob-refcount  Re-derive blob ref counts and sweep orphans (use with 'fix').
  --triggers       Recreate blob ref-count triggers (use with 'fix').
  --no-hints       Suppress hint-severity findings.

examples:
  siftd doctor                          # run all checks
  siftd doctor list                     # list available checks
  siftd doctor run                      # run all checks (explicit)
  siftd doctor run ingest-pending       # run specific check
  siftd doctor run check1 check2        # run multiple checks
  siftd doctor fix                      # show fix commands for issues
  siftd doctor fix --pending-tags       # clean up stale sessions/tags
  siftd doctor --json                   # output as JSON
  siftd doctor --strict                 # exit 1 on warnings (for CI)

legacy (still supported):
  siftd doctor checks                   # same as 'list'
  siftd doctor fixes                    # same as 'fix'
  siftd doctor ingest-pending           # same as 'run ingest-pending'

exit codes:
  0  no errors (or no warnings with --strict)
  1  errors found (or warnings with --strict)
```

## siftd db

```
siftd > db
Database operations (info, schema-version, backup, restore, vacuum, slice,
merge, send, receive, remote, push, pull)

usage: siftd db [-h] <command> ...

COMMANDS
  info            Show database file metadata and schema info
  schema-version  Show migration triage info: current version, target, pending
                  migrations
  stats           Show database statistics
  workspaces      List workspaces with conversation counts
  path            Show XDG paths
  vacuum          Compact database and optimize indexes
  backup          Create a consistent online backup
  restore         Restore database from a backup file
  slice           Export filtered conversations into a standalone database
  merge           Merge an external database (slice) into the main database
  receive         Receive a database from stdin and create-or-merge into the
                  local database
  process         Merge staged inbox payloads into the database
  sync-status     Report sync capabilities and inbox status (JSON)
  send            Slice the database and write binary SQLite to stdout
  remote          Manage sync remotes (add, list, remove)
  push            Push conversations to a sync remote
  pull            Pull conversations from a sync remote

Container-level operations on the siftd database.

Inspection:
  siftd db info                          # database file metadata
  siftd db schema-version                # migration triage info
  siftd db stats                         # full statistics
  siftd db workspaces                    # list workspaces
  siftd db path                          # show XDG paths
  siftd db sync-status                   # sync capabilities and inbox state

Maintenance:
  siftd db vacuum                        # compact database
  siftd db backup /tmp/siftd.db          # online backup
  siftd db restore /tmp/siftd.db         # restore from backup

Sync:
  siftd db slice out.db -w project       # export filtered subset
  siftd db merge laptop-slice.db         # merge slice into main DB
  siftd db send > slice.db               # send via stdout (SSH pipe)
  siftd db push alcove                   # push delta to remote
  siftd db pull alcove                   # pull delta from remote

Sync remotes:
  siftd db remote add alcove host:path   # register sync remote
  siftd db receive < slice.db            # receive via stdin (SSH pipe)
  siftd db process                       # process staged inbox payloads

Run 'siftd db <command> --help' for details.
```

## siftd embed

```
siftd > embed
Build and inspect the semantic-search index

usage: siftd embed [-h] [--rebuild] [--status] [--embed-db PATH] [--json]

OPTIONS
  --rebuild        Rebuild the entire index from scratch (instead of
                   incremental)
  --status         Show index stats: backend, model, coverage, staleness, size
  --embed-db PATH  Alternate embeddings database path
  --json           Output --status as JSON

Builds the embeddings index that powers semantic search. The backend
is config-driven (embed.backend); install the local backend or configure a remote
one with 'siftd install embed'.

examples:
  siftd embed                 # incremental: index new + changed, prune removed
  siftd embed --rebuild       # rebuild the whole index from scratch
  siftd embed --status        # backend, coverage, staleness, size
```

## siftd serve

```
siftd > serve
Serve the siftd database over HTTP for team sync.

usage: siftd serve [-h] [--host ADDR] [--port PORT] [--no-auth]
                   [--unsafe-public-no-auth]

OPTIONS
  --host ADDR              Bind address (default: 127.0.0.1)
  --port PORT              Listen port (default: 8484)
  --no-auth                Disable authentication (development only)
  --unsafe-public-no-auth  Allow binding a non-loopback address with NO
                           authentication. Dangerous: exposes the entire corpus
                           for read and write. Without this flag, a public bind
                           without [serve.auth] is refused.
```

## siftd auth

```
siftd > auth
Client-side token acquisition. `login` runs the OAuth device-code flow against
the configured [auth].issuer; the resulting token is stored and presented
automatically to a remote siftd serve.

usage: siftd auth [-h] <command> ...

COMMANDS
  login   Authorize via OAuth device-code flow
  status  Show stored credential status
  logout  Delete the stored credential

Run 'siftd auth <command> --help' for details.
```

## siftd install

```
siftd > install
Install optional dependencies or bundled components

usage: siftd install [-h] [--dry-run] [--scope {user,project}] [--harness NAME]
                     [{embed,serve,skill,plugin}]

ARGUMENTS
  {embed,serve,skill,plugin}  Component to install (skill: search workflow,
                              plugin: skill + hooks + commands, embed: semantic
                              search, serve: HTTP server)

OPTIONS
  --dry-run               Show what would be run without executing
  --scope {user,project}  Install scope: user (home dir) or project (current
                          dir)
  --harness NAME          Target harness for skill install (claude_code,
                          codex_cli, gemini_cli, pi_agent, copilot_cli, aider)

examples:
  siftd install skill                         # Claude Code skill (default)
  siftd install skill --harness codex_cli     # Codex CLI instructions
  siftd install skill --harness gemini_cli    # Gemini CLI instructions
  siftd install skill --harness pi_agent      # Pi Agent skill
  siftd install plugin                        # full Claude Code plugin
  siftd install plugin --scope project        # plugin for current project only
  siftd install embed                         # semantic search dependencies
  siftd install serve                         # HTTP server dependencies
```

## siftd config

```
siftd > config
View or modify config settings

usage: siftd config [-h] [--json] [{get,set,path,append,remove,tag-prefixes}]
                    [key] [value]

ARGUMENTS
  {get,set,path,append,remove,tag-prefixes}  Action to perform
  key                                        Config key (dotted path, e.g.,
                                             serve.host)
  value                                      Value to use (for 'set', 'append',
                                             'remove')

OPTIONS
  --json  JSON output (currently used by 'tag-prefixes')

examples:
  siftd config                        # show all config
  siftd config path                   # show config file path
  siftd config get serve.host             # get specific value
  siftd config set serve.port 9090        # set value
  siftd config append adapters.claude_code.locations ~/.claude/projects
  siftd config remove adapters.claude_code.locations ~/.claude/projects
  siftd config tag-prefixes               # show resolved tag-prefix table
  siftd config tag-prefixes --json        # same, as JSON
```
