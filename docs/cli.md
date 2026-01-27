# strata CLI Reference

_Auto-generated from `--help` output._

## Main

```
usage: strata [-h] [--db PATH]
              {ingest,status,ask,tag,tags,tools,query,backfill,path,config,adapters,copy,doctor,peek} ...

Aggregate and query LLM conversation logs

positional arguments:
  {ingest,status,ask,tag,tags,tools,query,backfill,path,config,adapters,copy,doctor,peek}
    ingest              Ingest logs from all sources
    status              Show database statistics
    ask                 Semantic search over conversations
    tag                 Apply a tag to a conversation (or other entity)
    tags                List all tags
    tools               Summarize tool usage by category
    query               List conversations with filters, or run SQL queries
    backfill            Backfill derived data from existing records
    path                Show XDG paths
    config              View or modify config settings
    adapters            List discovered adapters
    copy                Copy built-in resources for customization
    doctor              Run health checks and maintenance
    peek                Inspect live sessions from disk (bypasses SQLite)

options:
  -h, --help            show this help message and exit
  --db PATH             Database path (default:
                        /Users/kaygee/.local/share/strata/strata.db)
```

## ingest

```
usage: strata ingest [-h] [-v] [-p DIR]

options:
  -h, --help      show this help message and exit
  -v, --verbose   Show all files including skipped
  -p, --path DIR  Additional directories to scan (can be repeated)
```

## status

```
usage: strata status [-h]

options:
  -h, --help  show this help message and exit
```

## ask

```
usage: strata ask [-h] [-n LIMIT] [-v] [--full] [--context N] [--chrono]
                  [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE]
                  [--index] [--rebuild] [--backend NAME] [--embed-db PATH]
                  [--thread] [--embeddings-only] [--recall N]
                  [--role {user,assistant}] [--first] [--conversations]
                  [--refs [FILES]] [--threshold SCORE] [--json]
                  [--format NAME] [--no-exclude-active]
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
  --since DATE          Conversations started after this date
  --before DATE         Conversations started before this date
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

examples:
  # search
  strata ask "error handling"                        # basic semantic search
  strata ask -w myproject "auth flow"                # filter by workspace
  strata ask --since 2024-06 "testing"               # filter by date

  # refine
  strata ask "design decision" --thread              # narrative: top conversations expanded
  strata ask "why we chose X" --context 2            # ±2 surrounding exchanges
  strata ask "testing approach" --role user           # just your prompts, not responses
  strata ask "event sourcing" --conversations        # rank whole conversations, not chunks
  strata ask "when first discussed Y" --first        # earliest match above threshold
  strata ask --threshold 0.7 "architecture"          # only high-relevance results

  # inspect
  strata ask -v "chunking"                           # full chunk text
  strata ask --full "chunking"                       # complete prompt+response exchange
  strata ask --refs "authelia"                       # file references + content
  strata ask --refs HANDOFF.md "setup"               # filter refs to specific file

  # save useful results for future retrieval
  strata tag 01HX... research:auth                   # bookmark a conversation
  strata tag --last research:architecture            # tag most recent conversation
  strata query -l research:auth                      # retrieve tagged conversations

  # tuning
  strata ask --embeddings-only "chunking"            # skip FTS5, pure embeddings
  strata ask --recall 200 "error"                    # widen FTS5 candidate pool
  strata ask --chrono "chunking"                     # sort by time instead of score
```

## tag

```
usage: strata tag [-h] [-n N] [positional ...]

positional arguments:
  positional    [entity_type] entity_id tag

options:
  -h, --help    show this help message and exit
  -n, --last N  Tag N most recent conversations

examples:
  strata tag 01HX... important       # tag conversation (default)
  strata tag --last important        # tag most recent conversation
  strata tag --last 3 review         # tag 3 most recent conversations
  strata tag workspace 01HY... proj  # explicit entity type
  strata tag tool_call 01HZ... slow  # tag a tool call
```

## tags

```
usage: strata tags [-h]

options:
  -h, --help  show this help message and exit
```

## tools

```
usage: strata tools [-h] [--by-workspace] [--prefix PREFIX] [-n LIMIT]

options:
  -h, --help         show this help message and exit
  --by-workspace     Show breakdown by workspace
  --prefix PREFIX    Tag prefix to filter (default: shell:)
  -n, --limit LIMIT  Max workspaces for --by-workspace (default: 20)

examples:
  strata tools                    # shell command categories summary
  strata tools --by-workspace     # breakdown by workspace
  strata tools --prefix shell:    # filter by tag prefix
```

## query

```
usage: strata query [-h] [-v] [-n COUNT] [--latest] [--oldest] [-w SUBSTR]
                    [-m NAME] [--since DATE] [--before DATE] [-s QUERY]
                    [-t NAME] [-l NAME] [--tool-tag NAME] [--json] [--stats]
                    [--var KEY=VALUE]
                    [conversation_id] [sql_name]

positional arguments:
  conversation_id       Conversation ID for detail view, or 'sql' for SQL
                        query mode
  sql_name              SQL query name (when using 'sql' subcommand)

options:
  -h, --help            show this help message and exit
  -v, --verbose         Full table with all columns
  -n, --count COUNT     Number of conversations to show (0=all, default: 10)
  --latest              Sort by newest first (default)
  --oldest              Sort by oldest first
  -w, --workspace SUBSTR
                        Filter by workspace path substring
  -m, --model NAME      Filter by model name
  --since DATE          Conversations started after this date (ISO or YYYY-MM-
                        DD)
  --before DATE         Conversations started before this date
  -s, --search QUERY    Full-text search (FTS5 syntax)
  -t, --tool NAME       Filter by canonical tool name (e.g. shell.execute)
  -l, --tag NAME        Filter by conversation tag
  --tool-tag NAME       Filter by tool call tag (e.g. shell:test)
  --json                Output as JSON array
  --stats               Show summary totals after list
  --var KEY=VALUE       Substitute $KEY with VALUE in SQL (for 'sql'
                        subcommand)

examples:
  strata query                         # list recent conversations
  strata query -w myproject            # filter by workspace
  strata query -s "error handling"     # FTS5 search
  strata query --tool-tag shell:test   # conversations with test commands
  strata query -w proj --tool-tag shell:vcs  # combine filters
  strata query <id>                    # show conversation detail
  strata query sql                     # list available .sql files
  strata query sql cost                # run the 'cost' query
  strata query sql cost --var ws=proj  # run with variable substitution
```

## backfill

```
usage: strata backfill [-h] [--shell-tags]

options:
  -h, --help    show this help message and exit
  --shell-tags  Tag shell.execute calls with shell:* categories
```

## path

```
usage: strata path [-h]

options:
  -h, --help  show this help message and exit
```

## config

```
usage: strata config [-h] [{get,set,path}] [key] [value]

positional arguments:
  {get,set,path}  Action to perform
  key             Config key (dotted path, e.g., ask.formatter)
  value           Value to set (for 'set' action)

options:
  -h, --help      show this help message and exit

examples:
  strata config                        # show all config
  strata config path                   # show config file path
  strata config get ask.formatter      # get specific value
  strata config set ask.formatter verbose  # set value
```

## adapters

```
usage: strata adapters [-h]

options:
  -h, --help  show this help message and exit
```

## copy

```
usage: strata copy [-h] [--all] [--force] {adapter,query} [name]

positional arguments:
  {adapter,query}  Resource type to copy
  name             Resource name

options:
  -h, --help       show this help message and exit
  --all            Copy all resources of this type
  --force          Overwrite existing files

examples:
  strata copy adapter claude_code    # copy adapter to ~/.config/strata/adapters/
  strata copy adapter --all          # copy all built-in adapters
  strata copy query cost             # copy query to ~/.config/strata/queries/
```

## doctor

```
usage: strata doctor [-h] [subcommand]

positional arguments:
  subcommand  'checks' to list, 'fixes' to show fixes, or check name

options:
  -h, --help  show this help message and exit

examples:
  strata doctor                    # run all checks
  strata doctor checks             # list available checks
  strata doctor fixes              # show fix commands for issues
  strata doctor ingest-pending     # run specific check
```

## peek

```
usage: strata peek [-h] [-w SUBSTR] [--all] [--last N] [--tail] [--json]
                   [session_id]

positional arguments:
  session_id            Session ID prefix for detail view

options:
  -h, --help            show this help message and exit
  -w, --workspace SUBSTR
                        Filter by workspace name substring
  --all                 Include inactive sessions (not just last 2 hours)
  --last N              Number of exchanges to show (default: 5)
  --tail                Raw JSONL tail (last 20 lines)
  --json                Output as structured JSON

examples:
  strata peek                    # list active sessions (last 2 hours)
  strata peek --all              # list all sessions
  strata peek -w myproject        # filter by workspace name
  strata peek c520f862           # detail view for session
  strata peek c520 --last 10     # show last 10 exchanges
  strata peek c520 --tail        # raw JSONL tail
```
