# CLI Reference

_Auto-generated from `--help` output._

## siftd

```
usage: siftd [-h] [--version] [--db PATH]
             {ingest,status,ask,install,tag,tags,tools,query,backfill,path,config,adapters,copy,doctor,peek,export} ...

Aggregate and query LLM conversation logs

positional arguments:
  {ingest,status,ask,install,tag,tags,tools,query,backfill,path,config,adapters,copy,doctor,peek,export}
    ingest              Ingest logs from all sources
    status              Show database statistics
    ask                 Semantic search over conversations (requires [embed]
                        extra)
    install             Install optional dependencies
    tag                 Apply or remove a tag on a conversation (or other
                        entity)
    tags                List, inspect, rename, or delete tags
    tools               Summarize tool usage by category
    query               List conversations with filters, or run SQL queries
    backfill            Backfill derived data from existing records
    path                Show XDG paths
    config              View or modify config settings
    adapters            List discovered adapters
    copy                Copy built-in resources for customization
    doctor              Run health checks and maintenance
    peek                Inspect live sessions from disk (bypasses SQLite)
    export              Export conversations for PR review workflows

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  --db PATH             Database path (default:
                        /Users/kaygee/.local/share/siftd/siftd.db)
```

## siftd ingest

```
usage: siftd ingest [-h] [-v] [-p DIR] [-a NAME]

options:
  -h, --help          show this help message and exit
  -v, --verbose       Show all files including skipped
  -p, --path DIR      Additional directories to scan (can be repeated)
  -a, --adapter NAME  Only run specific adapter(s) (can be repeated)
```

## siftd status

```
usage: siftd status [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

## siftd ask

```
usage: siftd ask [-h] [-n LIMIT] [-v] [--full] [--context N] [--chrono]
                 [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE]
                 [--index] [--rebuild] [--backend NAME] [--embed-db PATH]
                 [--thread] [--embeddings-only] [--recall N]
                 [--role {user,assistant}] [--first] [--conversations]
                 [--refs [FILES]] [--threshold SCORE] [--json] [--format NAME]
                 [--no-exclude-active] [--include-derivative] [--no-diversity]
                 [--lambda FLOAT] [-l NAME] [--all-tags NAME] [--no-tag NAME]
                 [query ...]

positional arguments:
  query                 Natural language search query

options:
  -h, --help            show this help message and exit
  -n, --limit LIMIT     Max results (default: 10)
  -v, --verbose         Show full chunk text
  --full                Show complete prompt+response exchange
  --context N           Show ±N exchanges around match
  --chrono              Sort results by time instead of score
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -m, --model NAME      Filter by model name
  --since DATE          Conversations started after this date (ISO format:
                        YYYY-MM-DD)
  --before DATE         Conversations started before this date (ISO format:
                        YYYY-MM-DD)
  --index               Build/update embeddings index
  --rebuild             Rebuild embeddings index from scratch
  --backend NAME        Embedding backend (ollama, fastembed)
  --embed-db PATH       Alternate embeddings database path
  --thread              Two-tier narrative thread output: top conversations
                        expanded, rest as shortlist
  --embeddings-only     Skip FTS5 recall, use pure embeddings
  --recall N            FTS5 conversation recall limit (default: 80)
  --role {user,assistant}
                        Filter by source role (user prompts or assistant
                        responses)
  --first               Return chronologically earliest match above threshold
  --conversations       Aggregate scores per conversation, return ranked
                        conversations
  --refs [FILES]        Show file references; optionally filter by comma-
                        separated basenames
  --threshold SCORE     Filter results below this relevance score (e.g., 0.7)
  --json                Output as structured JSON
  --format NAME         Use named formatter (built-in or drop-in plugin)
  --no-exclude-active   Include results from active sessions (excluded by
                        default)
  --include-derivative  Include derivative conversations (siftd ask/query
                        results, excluded by default)
  --no-diversity        Disable MMR diversity reranking, use pure relevance
                        order
  --lambda FLOAT        MMR lambda: 1.0=pure relevance, 0.0=pure diversity
                        (default: 0.7)
  -l, --tag NAME        Filter by conversation tag (repeatable, OR logic)
  --all-tags NAME       Require all specified tags (AND logic)
  --no-tag NAME         Exclude conversations with this tag (NOT logic)

Note: Requires the [embed] extra. Install with: siftd install embed

examples:
  # search
  siftd ask "error handling"                        # basic semantic search
  siftd ask -w myproject "auth flow"                # filter by workspace
  siftd ask --since 2024-06 "testing"               # filter by date

  # refine
  siftd ask "design decision" --thread              # narrative: top conversations expanded
  siftd ask "why we chose X" --context 2            # ±2 surrounding exchanges
  siftd ask "testing approach" --role user           # just your prompts, not responses
  siftd ask "event sourcing" --conversations        # rank whole conversations, not chunks
  siftd ask "when first discussed Y" --first        # earliest match above threshold
  siftd ask --threshold 0.7 "architecture"          # only high-relevance results

  # inspect
  siftd ask -v "chunking"                           # full chunk text
  siftd ask --full "chunking"                       # complete prompt+response exchange
  siftd ask --refs "authelia"                       # file references + content
  siftd ask --refs HANDOFF.md "setup"               # filter refs to specific file

  # filter by tags
  siftd ask -l research:auth "auth flow"            # search within tagged conversations
  siftd ask -l research: -l useful: "pattern"       # OR — any research: or useful: tag
  siftd ask --all-tags important --all-tags reviewed "design"  # AND — must have both
  siftd ask -l research: --no-tag archived "auth"   # combine OR + NOT

  # save useful results for future retrieval
  siftd tag 01HX... research:auth                   # bookmark a conversation
  siftd tag --last research:architecture            # tag most recent conversation
  siftd query -l research:auth                      # retrieve tagged conversations

  # tuning
  siftd ask --embeddings-only "chunking"            # skip FTS5, pure embeddings
  siftd ask --recall 200 "error"                    # widen FTS5 candidate pool
  siftd ask --chrono "chunking"                     # sort by time instead of score
```

## siftd install

```
usage: siftd install [-h] [--dry-run] {embed}

positional arguments:
  {embed}     Optional extra to install

options:
  -h, --help  show this help message and exit
  --dry-run   Show what would be run without executing

examples:
  siftd install embed             # install semantic search dependencies
  siftd install embed --dry-run   # show what would be installed
```

## siftd tag

```
usage: siftd tag [-h] [-n N] [-r] [positional ...]

positional arguments:
  positional    [entity_type] entity_id tag [tag2 ...]

options:
  -h, --help    show this help message and exit
  -n, --last N  Tag N most recent conversations
  -r, --remove  Remove tag instead of applying

examples:
  siftd tag 01HX... important              # tag conversation (default)
  siftd tag 01HX... important review       # apply multiple tags at once
  siftd tag --last important               # tag most recent conversation
  siftd tag --last 3 review                # tag 3 most recent conversations
  siftd tag workspace 01HY... proj         # explicit entity type
  siftd tag tool_call 01HZ... slow         # tag a tool call
  siftd tag --remove 01HX... important     # remove tag from conversation
  siftd tag --remove --last important      # remove from most recent
  siftd tag -r workspace 01HY... proj      # remove from workspace
```

## siftd tags

```
usage: siftd tags [-h] [--prefix PREFIX] [-n LIMIT] [--rename OLD NEW]
                  [--delete NAME] [--force]
                  [name]

positional arguments:
  name               Tag name to drill into (shows conversations)

options:
  -h, --help         show this help message and exit
  --prefix PREFIX    Filter tag list by prefix (list view only)
  -n, --limit LIMIT  Max conversations to show in drill-down (default: 10)
  --rename OLD NEW   Rename a tag
  --delete NAME      Delete a tag and all associations
  --force            Force delete even if tag has associations

examples:
  siftd tags                                      # list all tags
  siftd tags --prefix research:                   # list tags by prefix
  siftd tags research:auth                        # show conversations with a tag
  siftd tags --rename important review:important   # rename tag
  siftd tags --delete old-tag                      # delete tag (refuses if applied)
  siftd tags --delete old-tag --force              # delete tag and all associations
```

## siftd tools

```
usage: siftd tools [-h] [--by-workspace] [--prefix PREFIX] [-n LIMIT] [--json]

options:
  -h, --help         show this help message and exit
  --by-workspace     Show breakdown by workspace
  --prefix PREFIX    Tag prefix to filter (default: shell:)
  -n, --limit LIMIT  Max workspaces for --by-workspace (default: 20)
  --json             Output as JSON

examples:
  siftd tools                    # shell command categories summary
  siftd tools --by-workspace     # breakdown by workspace
  siftd tools --prefix shell:    # filter by tag prefix
```

## siftd query

```
usage: siftd query [-h] [-v] [-n COUNT] [--oldest] [-w SUBSTR] [-m NAME]
                   [--since DATE] [--before DATE] [-s QUERY] [-t NAME]
                   [-l NAME] [--all-tags NAME] [--no-tag NAME]
                   [--tool-tag NAME] [--json] [--stats] [--var KEY=VALUE]
                   [conversation_id] [sql_name]

positional arguments:
  conversation_id       Conversation ID for detail view, or 'sql' for SQL
                        query mode
  sql_name              SQL query name (when using 'sql' subcommand)

options:
  -h, --help            show this help message and exit
  -v, --verbose         Full table with all columns
  -n, --count COUNT     Number of conversations to show (0=all, default: 10)
  --oldest              Sort by oldest first (default: newest first)
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -m, --model NAME      Filter by model name
  --since DATE          Conversations started after this date (ISO format:
                        YYYY-MM-DD)
  --before DATE         Conversations started before this date (ISO format:
                        YYYY-MM-DD)
  -s, --search QUERY    Full-text search (FTS5 syntax)
  -t, --tool NAME       Filter by canonical tool name (e.g. shell.execute)
  -l, --tag NAME        Filter by conversation tag (repeatable, OR logic)
  --all-tags NAME       Require all specified tags (AND logic)
  --no-tag NAME         Exclude conversations with this tag (NOT logic)
  --tool-tag NAME       Filter by tool call tag (e.g. shell:test)
  --json                Output as JSON array
  --stats               Show summary totals after list
  --var KEY=VALUE       Substitute $KEY with VALUE in SQL (for 'sql'
                        subcommand)

examples:
  siftd query                         # list recent conversations
  siftd query -w myproject            # filter by workspace
  siftd query -s "error handling"     # FTS5 search
  siftd query -l research:auth        # conversations tagged research:auth
  siftd query -l research: -l useful: # OR — any research: or useful: tag
  siftd query --all-tags important --all-tags reviewed  # AND — must have both
  siftd query -l research: --no-tag archived            # combine OR + NOT
  siftd query --tool-tag shell:test   # conversations with test commands
  siftd query <id>                    # show conversation detail
  siftd query sql                     # list available .sql files
  siftd query sql cost                # run the 'cost' query
  siftd query sql cost --var ws=proj  # run with variable substitution
```

## siftd backfill

```
usage: siftd backfill [-h] [--shell-tags] [--derivative-tags]

options:
  -h, --help         show this help message and exit
  --shell-tags       Tag shell.execute calls with shell:* categories
  --derivative-tags  Tag conversations containing siftd ask/query as
                     siftd:derivative
```

## siftd path

```
usage: siftd path [-h]

options:
  -h, --help  show this help message and exit
```

## siftd config

```
usage: siftd config [-h] [{get,set,path}] [key] [value]

positional arguments:
  {get,set,path}  Action to perform
  key             Config key (dotted path, e.g., ask.formatter)
  value           Value to set (for 'set' action)

options:
  -h, --help      show this help message and exit

examples:
  siftd config                        # show all config
  siftd config path                   # show config file path
  siftd config get ask.formatter      # get specific value
  siftd config set ask.formatter verbose  # set value
```

## siftd adapters

```
usage: siftd adapters [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

## siftd copy

```
usage: siftd copy [-h] [--all] [--force] {adapter,query} [name]

positional arguments:
  {adapter,query}  Resource type to copy
  name             Resource name

options:
  -h, --help       show this help message and exit
  --all            Copy all resources of this type
  --force          Overwrite existing files

examples:
  siftd copy adapter claude_code    # copy adapter to ~/.config/siftd/adapters/
  siftd copy adapter --all          # copy all built-in adapters
  siftd copy query cost             # copy query to ~/.config/siftd/queries/
```

## siftd doctor

```
usage: siftd doctor [-h] [--json] [--strict] [subcommand ...]

positional arguments:
  subcommand  list | run [checks...] | fix | <check-name>

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
  --strict    Exit 1 on warnings (not just errors). Useful for CI.

examples:
  siftd doctor                          # run all checks
  siftd doctor list                     # list available checks
  siftd doctor run                      # run all checks (explicit)
  siftd doctor run ingest-pending       # run specific check
  siftd doctor run check1 check2        # run multiple checks
  siftd doctor fix                      # show fix commands for issues
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

## siftd peek

```
usage: siftd peek [-h] [-w SUBSTR] [--all] [--limit N] [--last N] [--full]
                  [--chars N] [--tail] [--tail-lines N] [--json]
                  [session_id]

positional arguments:
  session_id            Session ID prefix for detail view

options:
  -h, --help            show this help message and exit
  -w, --workspace SUBSTR
                        Filter by workspace name substring
  --all                 Include inactive sessions (not just last 2 hours)
  --limit N             Maximum number of sessions to list
  --last N              Number of exchanges to show (default: 5, minimum: 1)
  --full                Show full text (no truncation)
  --chars N             Truncate text at N characters (default: 200)
  --tail                Raw JSONL tail (last 20 records)
  --tail-lines N        Number of records for --tail (default: 20)
  --json                Output as structured JSON

examples:
  siftd peek                    # list active sessions (last 2 hours)
  siftd peek --all              # list all sessions
  siftd peek --all --limit 50   # list all, but only first 50
  siftd peek -w myproject       # filter by workspace name
  siftd peek c520f862           # detail view for session
  siftd peek c520 --last 10     # show last 10 exchanges
  siftd peek c520 --full        # show full text (no truncation)
  siftd peek c520 --tail        # raw JSONL tail
  siftd peek c520 --tail --json # tail as JSON array

NOTE: Session content may contain sensitive information (API keys, credentials, etc.).
```

## siftd export

```
usage: siftd export [-h] [-n [N]] [-w SUBSTR] [-l NAME] [--exclude-tag NAME]
                    [--since DATE] [--before DATE] [-s QUERY]
                    [-f {prompts,exchanges,json}] [--prompts-only]
                    [--no-header] [-o FILE]
                    [conversation_id]

positional arguments:
  conversation_id       Conversation ID to export (prefix match)

options:
  -h, --help            show this help message and exit
  -n, --last [N]        Export N most recent sessions (default: 1 if no ID
                        given)
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -l, --tag NAME        Filter by tag (repeatable, OR logic)
  --exclude-tag NAME    Exclude sessions with this tag (repeatable)
  --since DATE          Sessions after this date (ISO format: YYYY-MM-DD)
  --before DATE         Sessions before this date (ISO format: YYYY-MM-DD)
  -s, --search QUERY    Full-text search filter
  -f, --format {prompts,exchanges,json}
                        Output format: prompts (default), exchanges, json
  --prompts-only        Omit response text and tool calls
  --no-header           Omit session metadata header
  -o, --output FILE     Write to file instead of stdout

examples:
  siftd export --last                   # export most recent session (prompts)
  siftd export --last 3                 # export last 3 sessions
  siftd export 01HX4G7K                 # export specific session (prefix match)
  siftd export -w myproject --since 2024-01-01  # filter by workspace and date
  siftd export -l decision:auth         # export tagged conversations
  siftd export --last --format json     # structured JSON output
  siftd export --last --format exchanges  # include response summaries
  siftd export --last --prompts-only    # omit tool call details
  siftd export --last --exclude-tag private  # exclude private sessions
  siftd export --last -o context.md     # write to file
```
