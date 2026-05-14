# CLI Reference

_Auto-generated from `--help` output._

## siftd

```
usage: siftd [-h] [--version] [--db PATH]
             {register,session-id,config,adapters,db,tag,id,query,ingest,backfill,migrate,copy,doctor,search,install,peek,export,serve,upgrade} ...

Aggregate and query LLM conversation logs

positional arguments:
  {register,session-id,config,adapters,db,tag,id,query,ingest,backfill,migrate,copy,doctor,search,install,peek,export,serve,upgrade}
    register            Register an active session for live tagging
    session-id          Print the session ID for the current workspace
    config              View or modify config settings
    adapters            List discovered adapters
    db                  Database operations (info, schema-version, backup,
                        restore, vacuum, slice, merge, send, receive, remote,
                        push, pull)
    tag                 Manage tags: apply, remove, list, rename, delete
    id                  Classify a ULID and show its type and context
    query               List and filter conversations by metadata
    ingest              Ingest logs from all sources
    backfill            Backfill derived data from existing records
    migrate             Run data migrations
    copy                Copy built-in resources for customization
    doctor              Run health checks and maintenance
    search              Search conversations (auto-selects FTS5 or semantic
                        based on what's installed)
    install             Install optional dependencies or bundled components
    peek                Inspect live sessions from disk (bypasses SQLite)
    export              Export conversations as markdown or JSON
    serve               Start the HTTP team sync server
    upgrade             Check for and install updates

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --db PATH             Database path (default:
                        /Users/kaygee/.local/share/siftd/siftd.db)
```

## siftd register

```
usage: siftd register [-h] --session ID --adapter NAME [--workspace PATH]

options:
  -h, --help            show this help message and exit
  --session, -s ID      Harness session ID
  --adapter, -a NAME    Adapter name (e.g., claude_code)
  --workspace, -w PATH  Workspace path (default: current directory)

examples:
  siftd register --session abc123 --adapter claude_code
  siftd register --session abc123 --adapter claude_code --workspace /path/to/project
```

## siftd session-id

```
usage: siftd session-id [-h] [--workspace PATH]

options:
  -h, --help            show this help message and exit
  --workspace, -w PATH  Workspace path (default: current directory)

examples:
  siftd session-id                    # print session ID for current directory
  siftd session-id --workspace /path  # print session ID for specific workspace

Exits with code 1 if no session ID found (for scripting).
```

## siftd config

```
usage: siftd config [-h] [--json]
                    [{get,set,path,append,remove,tag-prefixes}] [key] [value]

positional arguments:
  {get,set,path,append,remove,tag-prefixes}
                        Action to perform
  key                   Config key (dotted path, e.g., serve.host)
  value                 Value to use (for 'set', 'append', 'remove')

options:
  -h, --help            show this help message and exit
  --json                JSON output (currently used by 'tag-prefixes')

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

## siftd adapters

```
usage: siftd adapters [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

## siftd db

```
usage: siftd db [-h]
                {info,schema-version,stats,workspaces,path,vacuum,backup,restore,slice,merge,receive,process,sync-status,send,remote,push,pull} ...

positional arguments:
  {info,schema-version,stats,workspaces,path,vacuum,backup,restore,slice,merge,receive,process,sync-status,send,remote,push,pull}
    info                Show database file metadata and schema info
    schema-version      Show migration triage info: current version, target,
                        pending migrations
    stats               Show database statistics
    workspaces          List workspaces with conversation counts
    path                Show XDG paths
    vacuum              Compact database and optimize indexes
    backup              Create a consistent online backup
    restore             Restore database from a backup file
    slice               Export filtered conversations into a standalone
                        database
    merge               Merge an external database (slice) into the main
                        database
    receive             Receive a database from stdin and create-or-merge into
                        the local database
    process             Merge staged inbox payloads into the database
    sync-status         Report sync capabilities and inbox status (JSON)
    send                Slice the database and write binary SQLite to stdout
    remote              Manage sync remotes (add, list, remove)
    push                Push conversations to a sync remote
    pull                Pull conversations from a sync remote

options:
  -h, --help            show this help message and exit

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
```

## siftd tag

```
usage: siftd tag [-h] [-n [N]] [-r] [--session ID] [--current]
                 [--exchange INDEX | --last-prompt | --last-response |
                 --last-exchange | --last-tool-call] [--prefix PREFIX]
                 [--limit LIMIT] [--force] [--by-workspace] [--json]
                 [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE]
                 [-l NAME] [--all-tags NAME] [--no-tag NAME] [--on KIND]
                 [--owner USER]
                 [positional ...]

Apply, remove, list, rename, or delete tags.

positional arguments:
  positional            [entity_type] entity_id tag [tag2 ...] | apply |
                        remove | list | rename | delete

options:
  -h, --help            show this help message and exit
  -n, --last, --latest [N]
                        Tag N most recent conversations (default: 1 if flag
                        used without N)
  -r, --remove          Remove tag instead of applying
  --session ID          Queue tag for a live session (applied at ingest)
  --current             Auto-detect current session (falls back to --last)
  --exchange INDEX      Tag specific exchange (1-based, requires --session)
  --last-prompt         Tag the last prompt of the session (requires
                        --session/--current)
  --last-response       Tag the last response of the session (requires
                        --session/--current)
  --last-exchange       Tag the last exchange of the session (requires
                        --session/--current)
  --last-tool-call      Tag the last tool_call of the session (requires
                        --session/--current)
  --prefix PREFIX       Filter tag list by prefix (use with 'tag list')
  --limit LIMIT         Result cap (drill-down: default 10, --by-workspace:
                        default 20 workspaces)
  --force               Force delete even if tag has associations (use with
                        'tag delete')
  --by-workspace        Group tag counts by workspace (use with 'tag list').
                        Counts only event-backed tags (tool_call, prompt,
                        response, exchange); conversation-level tags are
                        excluded. Composes with --on, --prefix, -w, --owner,
                        --all-tags, --limit only.
  --json                Output as JSON (use with 'tag list --by-workspace')

filtering:
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -m, --model NAME      Filter by model name
  --since DATE          Conversations started after this date (YYYY-MM-DD, 7d,
                        1w, yesterday, today)
  --before DATE         Conversations started before this date (YYYY-MM-DD,
                        7d, 1w, yesterday, today)
  --owner USER          Filter to conversations owned by this user

tag filtering:
  -l, --tag NAME        Filter by tag (repeatable, OR logic)
  --all-tags NAME       Require all specified tags (AND logic)
  --no-tag NAME         Exclude conversations with this tag (NOT logic)
  --on KIND             Scope tag filters to a specific target kind
                        (repeatable). Default: match tags on any kind
                        (conversation, prompt, response, tool_call, exchange).

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

## siftd id

```
usage: siftd id [-h] [--json] ulid

positional arguments:
  ulid        ULID or ULID prefix to classify

options:
  -h, --help  show this help message and exit
  --json      Output as JSON

examples:
  siftd id 01HX4G7K9                   # identify a conversation or event
  siftd id 01HX4G7K9 --json            # structured classification
```

## siftd query

```
usage: siftd query [-h] [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE]
                   [-l NAME] [--all-tags NAME] [--no-tag NAME] [--on KIND]
                   [-t NAME] [--tool-tag NAME] [--owner USER] [-n LIMIT] [-v]
                   [--oldest] [--json] [--stats] [--no-hints] [--exchanges N]
                   [-b] [--summary] [-F] [--chars N] [--thinking]
                   [--tools [FILTER]] [--tool-chars N] [--neighbors]
                   [--var KEY=VALUE]
                   [conversation_id] [sql_name]

positional arguments:
  conversation_id       Conversation ID for detail view, or 'sql' for SQL
                        query mode
  sql_name              SQL query name (when using 'sql' subcommand)

options:
  -h, --help            show this help message and exit

filtering:
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -m, --model NAME      Filter by model name
  --since DATE          Conversations started after this date (YYYY-MM-DD, 7d,
                        1w, yesterday, today)
  --before DATE         Conversations started before this date (YYYY-MM-DD,
                        7d, 1w, yesterday, today)
  -t, --tool NAME       Filter by canonical tool name (e.g. shell.execute)
  --owner USER          Filter to conversations owned by this user

tag filtering:
  -l, --tag NAME        Filter by tag (repeatable, OR logic)
  --all-tags NAME       Require all specified tags (AND logic)
  --no-tag NAME         Exclude conversations with this tag (NOT logic)
  --on KIND             Scope tag filters to a specific target kind
                        (repeatable). Default: match tags on any kind
                        (conversation, prompt, response, tool_call, exchange).
  --tool-tag NAME       Filter by tool call tag (e.g. shell:test)

output:
  -n, --limit LIMIT     Number of conversations to show (0=all, default: 10)
  -v, --verbose         Full table with all columns
  --oldest              Sort by oldest first (default: newest first)
  --json                Output as JSON array
  --stats               Show summary totals after list
  --no-hints            Suppress hint-severity caveat findings.

detail view:
  --exchanges N         Number of turns to show (default: all)
  -b, --brief           Compact detail view (80 char truncation)
  --summary             Summary only (metadata, no turns)
  -F, --full            Full text (no truncation)
  --chars N             Truncate text at N characters (default: no truncation)
  --thinking            Show model thinking/reasoning blocks
  --tools [FILTER]      Show tool inputs/results (optional filter: tool name
                        prefix or 'errors')
  --tool-chars N        Truncate tool input/result at N characters (default:
                        120)
  --neighbors           Include prev_event_id/next_event_id in event detail
                        output

sql queries:
  --var KEY=VALUE       Substitute $KEY with VALUE in SQL

List and filter conversations by metadata (workspace, model, date, tags).
For semantic content search, use: siftd search <query>

Conversation IDs displayed in lists are truncated to 8 characters; use the full 26-character ID
to query a specific conversation.

examples:
  siftd query                         # list recent conversations
  siftd query -n 20                   # list 20 conversations
  siftd query -w myproject            # filter by workspace
  siftd query -l research:auth        # conversations tagged research:auth
  siftd query -l research: -l useful: # OR — any research: or useful: tag
  siftd query --all-tags important --all-tags reviewed  # AND — must have both
  siftd query -l research: --no-tag archived            # combine OR + NOT
  siftd query --tool-tag shell:test   # conversations with test commands
  siftd query <id>                    # show conversation detail
  siftd query <id> --summary          # metadata only, no exchanges
  siftd query <id> --exchanges 5      # last 5 exchanges
  siftd query <id> --brief            # compact detail view (80 char truncation)
  siftd query <id> -b                 # short alias for --brief
  siftd query <id> --full             # full text, no truncation
  siftd query <id> -F                 # short alias for --full
  siftd query sql                     # list available .sql files
  siftd query sql cost                # run the 'cost' query
  siftd query sql cost --var ws=proj  # run with variable substitution
```

## siftd ingest

```
usage: siftd ingest [-h] [-q | -v] [-p DIR] [-a NAME] [--json] [--rebuild-fts]

options:
  -h, --help          show this help message and exit
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

## siftd backfill

```
usage: siftd backfill [-h] [--shell-tags] [--derivative-tags]
                      [--filter-binary] [--git-remote] [--dry-run]

options:
  -h, --help         show this help message and exit
  --shell-tags       Tag shell.execute calls with shell:* categories
  --derivative-tags  Tag conversations containing siftd search/query as
                     siftd:derivative
  --filter-binary    Filter binary content (images, base64) from existing
                     blobs
  --git-remote       Backfill git remote URLs for workspaces missing them (use
                     'siftd migrate --merge-workspaces' to also collapse
                     duplicates)
  --dry-run          Preview changes without applying (use with --filter-
                     binary or --git-remote)

examples:
  siftd backfill                    # backfill response attributes (cache tokens)
  siftd backfill --shell-tags       # categorize shell commands as shell:git, shell:test, etc.
  siftd backfill --derivative-tags  # mark siftd-generated conversations
  siftd backfill --filter-binary    # filter binary content from existing blobs
  siftd backfill --filter-binary --dry-run  # preview what would be filtered
  siftd backfill --git-remote       # backfill git remote URLs for workspaces missing them
```

## siftd migrate

```
usage: siftd migrate [-h] [--merge-workspaces] [--dry-run] [-v]

options:
  -h, --help          show this help message and exit
  --merge-workspaces  Backfill git remote URLs and merge duplicate workspaces
  --dry-run           Show what would be done without making changes
  -v, --verbose       Verbose output

examples:
  siftd migrate                              # show workspace identity status
  siftd migrate --merge-workspaces           # backfill git remotes and merge duplicates
  siftd migrate --merge-workspaces --dry-run # preview what would be merged
  siftd migrate --merge-workspaces -v        # verbose output
```

## siftd copy

```
usage: siftd copy [-h] [--all] [--force] {adapter,query,formatter} [name]

positional arguments:
  {adapter,query,formatter}
                        Resource type to copy
  name                  Resource name

options:
  -h, --help            show this help message and exit
  --all                 Copy all resources of this type
  --force               Overwrite existing files

examples:
  siftd copy adapter claude_code    # copy adapter to ~/.config/siftd/adapters/
  siftd copy adapter --all          # copy all built-in adapters
  siftd copy query cost             # copy query to ~/.config/siftd/queries/
  siftd copy formatter markdown     # copy formatter to ~/.config/siftd/formatters/
```

## siftd doctor

```
usage: siftd doctor [-h] [--json] [--strict] [--pending-tags] [--deep]
                    [--fast] [--blob-refcount] [--triggers] [--no-hints]
                    [subcommand ...]

positional arguments:
  subcommand       list | run [checks...] | fix | <check-name>

options:
  -h, --help       show this help message and exit
  --json           Output as JSON
  --strict         Exit 1 on warnings (not just errors). Useful for CI.
  --pending-tags   Clean up stale sessions and orphaned pending tags (use with
                   'fix')
  --deep           Include deep integrity checks (slower).
  --fast           Run only fast checks (skips slow and deep).
  --blob-refcount  Re-derive blob ref counts and sweep orphans (use with
                   'fix').
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

## siftd search

```
usage: siftd search [-h] [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE]
                    [-l NAME] [--all-tags NAME] [--no-tag NAME] [--on KIND]
                    [--owner USER] [-n LIMIT] [-v] [--full] [--context N]
                    [--json] [--format NAME] [--select SELECTOR]
                    [--sort ORDER] [--mode MODE] [--refs [FILES]] [--fts]
                    [--semantic] [--embeddings-only] [--recall N]
                    [--threshold SCORE] [--raw-fts] [--no-diversity]
                    [--lambda FLOAT] [--recency] [--recency-half-life DAYS]
                    [--recency-max-boost MULT] [--no-exclude-active]
                    [--include-derivative] [--index] [--rebuild]
                    [--backend NAME] [--embed-db PATH]
                    [query ...]

positional arguments:
  query                 Natural language search query

options:
  -h, --help            show this help message and exit

filtering:
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -m, --model NAME      Filter by model name
  --since DATE          Conversations started after this date (YYYY-MM-DD, 7d,
                        1w, yesterday, today)
  --before DATE         Conversations started before this date (YYYY-MM-DD,
                        7d, 1w, yesterday, today)
  --owner USER          Filter to conversations owned by this user

tag filtering:
  -l, --tag NAME        Filter by tag (repeatable, OR logic)
  --all-tags NAME       Require all specified tags (AND logic)
  --no-tag NAME         Exclude conversations with this tag (NOT logic)
  --on KIND             Scope tag filters to a specific target kind
                        (repeatable). Default: match tags on any kind
                        (conversation, prompt, response, tool_call, exchange).

output:
  -n, --limit LIMIT     Max results (default: 10)
  -v, --verbose         Show full chunk text
  --full                Show complete prompt+response exchange
  --context N           Show ±N exchanges around match
  --json                Output as structured JSON
  --format NAME         Use named formatter (built-in or drop-in plugin)

result modes:
  --select SELECTOR     Result selector: all (default) or first
                        (chronologically earliest match above threshold)
  --sort ORDER          Sort order: score (default, relevance) or time
                        (chronological). Incompatible with
                        --mode=thread/conversations.
  --mode MODE           Render mode: chunks (default), thread (narrative
                        drill-down), or conversations (aggregated ranking)
  --refs [FILES]        Show file references; optionally filter by comma-
                        separated basenames

search mode:
  --fts                 Force FTS5 keyword search (no embeddings)
  --semantic            Force semantic search (requires embeddings)

search tuning:
  --embeddings-only     Skip FTS5 recall, use pure embeddings
  --recall N            FTS5 conversation recall limit (default: 80)
  --threshold SCORE     Filter results below this score (e.g., 0.7)
  --raw-fts             Pass query directly to FTS5 without tokenization
                        (advanced: skips OR fallback)

diversity:
  --no-diversity        Disable MMR reranking for deterministic pure relevance
                        order
  --lambda FLOAT        MMR lambda: 1.0=relevance, 0.0=diversity (default:
                        0.7)

recency:
  --recency             Boost recent results (exponential decay, mild 15%
                        boost)
  --recency-half-life DAYS
                        Days until recency boost decays to half (default: 30)
  --recency-max-boost MULT
                        Max boost multiplier for today's results (default:
                        1.15)

scope:
  --no-exclude-active   Include results from active sessions (excluded by
                        default)
  --include-derivative  Include derivative conversations (siftd search/query
                        results)

index management:
  --index               Build/update embeddings index
  --rebuild             Rebuild embeddings index from scratch
  --backend NAME        Embedding backend (ollama, fastembed)
  --embed-db PATH       Alternate embeddings database path

Unified search: auto-selects the best available search mechanism.
- With embeddings installed: hybrid search (FTS5 recall + semantic reranking)
- Without embeddings: FTS5 keyword search (install embeddings: siftd install embed)

examples:
  # search (auto-selects best available mode)
  siftd search "error handling"                        # hybrid or FTS5 (auto)
  siftd search -w myproject "auth flow"                # filter by workspace
  siftd search --since 2024-06 "testing"               # filter by date

  # explicit mode selection
  siftd search --fts "error handling"                  # force FTS5 keyword search
  siftd search --semantic "auth flow"                  # force semantic search

  # refine
  siftd search "design decision" --mode=thread         # narrative: top conversations expanded
  siftd search "why we chose X" --context 2            # ±2 surrounding exchanges
  siftd search "event sourcing" --mode=conversations   # rank whole conversations, not chunks
  siftd search "when first discussed Y" --select=first # earliest match above threshold
  siftd search --threshold 0.7 "architecture"          # only high-relevance results

  # inspect
  siftd search -v "chunking"                           # full chunk text
  siftd search --full "chunking"                       # complete prompt+response exchange
  siftd search --refs "authelia"                       # file references + content
  siftd search --refs HANDOFF.md "setup"               # filter refs to specific file

  # filter by tags
  siftd search -l research:auth "auth flow"            # search within tagged conversations
  siftd search -l research: -l useful: "pattern"       # OR — any research: or useful: tag
  siftd search --all-tags important --all-tags reviewed "design"  # AND — must have both
  siftd search -l research: --no-tag archived "auth"   # combine OR + NOT

  # save useful results for future retrieval
  siftd tag 01HX... research:auth                   # bookmark a conversation
  siftd tag --last research:architecture            # tag most recent conversation
  siftd query -l research:auth                      # retrieve tagged conversations

  # tuning
  siftd search --embeddings-only "chunking"            # skip FTS5, pure embeddings
  siftd search --recall 200 "error"                    # widen FTS5 candidate pool
  siftd search --sort=time "chunking"                   # sort by time instead of score

  # diversity vs relevance (MMR reranking)
  siftd search --no-diversity "chunking"               # pure relevance order (deterministic)
  siftd search --lambda 0.5 "design"                   # more diverse results (less redundancy)
  siftd search --json "auth" | jq '.results[0].breakdown'  # score component breakdown
```

## siftd install

```
usage: siftd install [-h] [--dry-run] [--scope {user,project}]
                     [--harness NAME]
                     [{embed,serve,skill,plugin}]

positional arguments:
  {embed,serve,skill,plugin}
                        Component to install (skill: search workflow, plugin:
                        skill + hooks + commands, embed: semantic search,
                        serve: HTTP server)

options:
  -h, --help            show this help message and exit
  --dry-run             Show what would be run without executing
  --scope {user,project}
                        Install scope: user (home dir) or project (current
                        dir)
  --harness NAME        Target harness for skill install (claude_code,
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

## siftd peek

```
usage: siftd peek [-h] [-w SUBSTR] [--branch SUBSTR] [--all] [-n N]
                  [--exchanges N] [-b] [-F] [--chars N] [--thinking] [--tools]
                  [-f] [--timeout SECONDS] [--tail] [--tail-lines N] [--json]
                  [--main-only] [--children ID] [--last-response]
                  [--last-prompt]
                  [session_id]

positional arguments:
  session_id            Session ID prefix for detail view

options:
  -h, --help            show this help message and exit
  -w, --workspace SUBSTR
                        Filter by workspace name substring
  --branch SUBSTR       Filter by worktree branch substring
  --all                 Include inactive sessions (not just last 2 hours)
  -n, --limit N         Maximum number of sessions to list (default: 10)
  --exchanges N         Detail mode: number of exchanges to show (default: 5)
  -b, --brief           Compact detail/follow view (80 char truncation)
  -F, --full            Show full text (no truncation)
  --chars N             Truncate text at N characters (default: no truncation)
  --thinking            Show model thinking/reasoning blocks inline when
                        available
  --tools               Show tool inputs/results inline when available
  -f, --follow          Follow a live session in real time (like tail -f)
  --timeout SECONDS     Exit after SECONDS of wall-clock time (for use with
                        --follow)
  --tail                Raw JSONL tail (last 20 records)
  --tail-lines N        Number of records for --tail (default: 20)
  --json                Output as structured JSON
  --main-only           Only show main sessions (exclude subagents)
  --children ID         Show only children of the specified parent session
  --last-response       Output only the last assistant response (raw text, no
                        formatting)
  --last-prompt         Output only the last user prompt (raw text, no
                        formatting)

examples:
  siftd peek                    # list latest 10 sessions
  siftd peek -n 5               # list latest 5 sessions
  siftd peek --all              # list all sessions (no time limit)
  siftd peek --all -n 50        # list all, but only first 50
  siftd peek -w myproject       # filter by workspace name
  siftd peek c520f862           # detail view for session (last 5 exchanges)
  siftd peek c520 --exchanges 10  # show last 10 exchanges
  siftd peek c520 --thinking    # show thinking blocks inline
  siftd peek c520 --tools       # show tool inputs/results inline when available
  siftd peek c520 --brief       # compact detail view (80 char truncation)
  siftd peek c520 -b            # short alias for --brief
  siftd peek c520 --full        # show full text (no truncation)
  siftd peek c520 -F            # short alias for --full
  siftd peek c520 --tail        # raw JSONL tail
  siftd peek c520 --tail --json # tail as JSON array
  siftd peek --main-only        # exclude subagent sessions
  siftd peek --children abc123  # show children of parent session
  siftd peek --last-response    # output last assistant response (raw text)
  siftd peek --last-prompt      # output last user prompt (raw text)
  siftd peek c520 --last-response  # last response from specific session
  siftd peek c520 --follow      # follow a live session in real time
  siftd peek --follow            # follow most recent active session
  siftd peek --follow --json    # follow as NDJSON (one object per line)

NOTE: Session content may contain sensitive information (API keys, credentials, etc.).
```

## siftd export

```
usage: siftd export [-h] [-n [N]] [-w SUBSTR] [--since DATE] [--before DATE]
                    [-l NAME] [--no-tag NAME] [--on KIND] [-s QUERY]
                    [--owner USER] [--thinking] [--tools] [-b] [-F] [--json]
                    [--no-header] [-o FILE]
                    [conversation_id]

positional arguments:
  conversation_id       Conversation ID (prefix match)

options:
  -h, --help            show this help message and exit
  -n, --last, --latest [N]
                        Export N most recent sessions (default: 1 if no ID
                        given)

filtering:
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  --since DATE          Conversations started after this date (YYYY-MM-DD, 7d,
                        1w, yesterday, today)
  --before DATE         Conversations started before this date (YYYY-MM-DD,
                        7d, 1w, yesterday, today)
  -s, --search QUERY    Full-text search filter
  --owner USER          Filter to conversations owned by this user

tag filtering:
  -l, --tag NAME        Filter by tag (repeatable, OR logic)
  --no-tag NAME         Exclude conversations with this tag (NOT logic)
  --on KIND             Scope tag filters to a specific target kind
                        (repeatable). Default: match tags on any kind
                        (conversation, prompt, response, tool_call, exchange).

rendering:
  --thinking            Expand thinking/reasoning blocks (default:
                        placeholder)
  --tools               Expand tool inputs and results (default: summary)
  -b, --brief           Condensed output (truncate long text)
  -F, --full            Full output: thinking + tools, no truncation
  --json                Structured JSON output
  --no-header           Omit session metadata header
  -o, --output FILE     Write to file instead of stdout

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

## siftd serve

```
usage: siftd serve [-h] [--host ADDR] [--port PORT] [--no-auth]

Serve the siftd database over HTTP for team sync.

options:
  -h, --help   show this help message and exit
  --host ADDR  Bind address (default: 127.0.0.1)
  --port PORT  Listen port (default: 8484)
  --no-auth    Disable authentication (development only)
```

## siftd upgrade

```
usage: siftd upgrade [-h] [--check]

options:
  -h, --help  show this help message and exit
  --check     Check for updates without installing
```
