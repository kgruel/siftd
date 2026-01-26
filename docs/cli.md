# tbd CLI Reference

_Auto-generated from `--help` output._

## Main

```
usage: tbd [-h] [--db PATH]
           {ingest,status,ask,tag,tags,tools,query,backfill,path,config,adapters,copy,doctor} ...

Aggregate and query LLM conversation logs

positional arguments:
  {ingest,status,ask,tag,tags,tools,query,backfill,path,config,adapters,copy,doctor}
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

options:
  -h, --help            show this help message and exit
  --db PATH             Database path (default:
                        /Users/kaygee/.local/share/tbd/tbd.db)
```

## ingest

```
usage: tbd ingest [-h] [-v] [-p DIR]

options:
  -h, --help      show this help message and exit
  -v, --verbose   Show all files including skipped
  -p, --path DIR  Additional directories to scan (can be repeated)
```

## status

```
usage: tbd status [-h]

options:
  -h, --help  show this help message and exit
```

## ask

```
usage: tbd ask [-h] [-n LIMIT] [-v] [--full] [--context N] [--chrono]
               [-w SUBSTR] [-m NAME] [--since DATE] [--before DATE] [--index]
               [--rebuild] [--backend NAME] [--embed-db PATH] [--thread]
               [--embeddings-only] [--recall N] [--role {user,assistant}]
               [--first] [--conversations] [--refs [FILES]]
               [--threshold SCORE] [--json] [--format NAME]
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

examples:
  tbd ask "chunking"                 # hybrid: FTS5 recall → embeddings rerank
  tbd ask -v "chunking"              # full chunk text
  tbd ask --full "chunking"          # complete exchange from DB
  tbd ask --context 3 "chunking"     # ±3 exchanges around match
  tbd ask --chrono "chunking"        # sort by time instead of score
  tbd ask --embeddings-only "chunking"  # skip FTS5, pure embeddings
  tbd ask --thread "chunking"         # narrative thread: top convos + shortlist
  tbd ask --recall 200 "error"       # widen FTS5 candidate pool
  tbd ask -w myproject "architecture"   # FTS5 + workspace filter
  tbd ask --role user "chunking"     # only search user prompts
  tbd ask --first "error handling"   # earliest mention above threshold
  tbd ask --conversations "testing"  # rank conversations, not chunks
  tbd ask --refs "authelia"          # show file ref annotations + content dump
  tbd ask --refs HANDOFF.md "setup"  # content dump filtered to specific file
  tbd ask --threshold 0.7 "error"    # only results with score >= 0.7
```

## tag

```
usage: tbd tag [-h] [-n N] [positional ...]

positional arguments:
  positional    [entity_type] entity_id tag

options:
  -h, --help    show this help message and exit
  -n, --last N  Tag N most recent conversations

examples:
  tbd tag 01HX... important       # tag conversation (default)
  tbd tag --last important        # tag most recent conversation
  tbd tag --last 3 review         # tag 3 most recent conversations
  tbd tag workspace 01HY... proj  # explicit entity type
  tbd tag tool_call 01HZ... slow  # tag a tool call
```

## tags

```
usage: tbd tags [-h]

options:
  -h, --help  show this help message and exit
```

## tools

```
usage: tbd tools [-h] [--by-workspace] [--prefix PREFIX] [-n LIMIT]

options:
  -h, --help         show this help message and exit
  --by-workspace     Show breakdown by workspace
  --prefix PREFIX    Tag prefix to filter (default: shell:)
  -n, --limit LIMIT  Max workspaces for --by-workspace (default: 20)

examples:
  tbd tools                    # shell command categories summary
  tbd tools --by-workspace     # breakdown by workspace
  tbd tools --prefix shell:    # filter by tag prefix
```

## query

```
usage: tbd query [-h] [-v] [-n COUNT] [--latest] [--oldest] [-w SUBSTR]
                 [-m NAME] [--since DATE] [--before DATE] [-s QUERY] [-t NAME]
                 [-l NAME] [--tool-tag NAME] [--json] [--stats]
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
  tbd query                         # list recent conversations
  tbd query -w myproject            # filter by workspace
  tbd query -s "error handling"     # FTS5 search
  tbd query --tool-tag shell:test   # conversations with test commands
  tbd query -w proj --tool-tag shell:vcs  # combine filters
  tbd query <id>                    # show conversation detail
  tbd query sql                     # list available .sql files
  tbd query sql cost                # run the 'cost' query
  tbd query sql cost --var ws=proj  # run with variable substitution
```

## backfill

```
usage: tbd backfill [-h] [--shell-tags]

options:
  -h, --help    show this help message and exit
  --shell-tags  Tag shell.execute calls with shell:* categories
```

## path

```
usage: tbd path [-h]

options:
  -h, --help  show this help message and exit
```

## config

```
usage: tbd config [-h] [{get,set,path}] [key] [value]

positional arguments:
  {get,set,path}  Action to perform
  key             Config key (dotted path, e.g., ask.formatter)
  value           Value to set (for 'set' action)

options:
  -h, --help      show this help message and exit

examples:
  tbd config                        # show all config
  tbd config path                   # show config file path
  tbd config get ask.formatter      # get specific value
  tbd config set ask.formatter verbose  # set value
```

## adapters

```
usage: tbd adapters [-h]

options:
  -h, --help  show this help message and exit
```

## copy

```
usage: tbd copy [-h] [--all] [--force] {adapter,query} [name]

positional arguments:
  {adapter,query}  Resource type to copy
  name             Resource name

options:
  -h, --help       show this help message and exit
  --all            Copy all resources of this type
  --force          Overwrite existing files

examples:
  tbd copy adapter claude_code    # copy adapter to ~/.config/tbd/adapters/
  tbd copy adapter --all          # copy all built-in adapters
  tbd copy query cost             # copy query to ~/.config/tbd/queries/
```

## doctor

```
usage: tbd doctor [-h] [subcommand]

positional arguments:
  subcommand  'checks' to list, 'fixes' to show fixes, or check name

options:
  -h, --help  show this help message and exit

examples:
  tbd doctor                    # run all checks
  tbd doctor checks             # list available checks
  tbd doctor fixes              # show fix commands for issues
  tbd doctor ingest-pending     # run specific check
```
