# tbd CLI Reference

_Auto-generated from `--help` output._

## Main

```
usage: tbd [-h] [--db PATH]
           {ingest,status,ask,label,labels,query,backfill,path} ...

Aggregate and query LLM conversation logs

positional arguments:
  {ingest,status,ask,label,labels,query,backfill,path}
    ingest              Ingest logs from all sources
    status              Show database statistics
    ask                 Semantic search over conversations
    label               Apply a label to an entity
    labels              List all labels
    query               List conversations with filters, or run SQL queries
    backfill            Backfill response attributes from raw files
    path                Show XDG paths

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
               [--threshold SCORE]
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

## label

```
usage: tbd label [-h] {conversation,workspace} entity_id label

positional arguments:
  {conversation,workspace}
                        Entity type
  entity_id             Entity ID (ULID)
  label                 Label name

options:
  -h, --help            show this help message and exit
```

## labels

```
usage: tbd labels [-h]

options:
  -h, --help  show this help message and exit
```

## query

```
usage: tbd query [-h] [-v] [-n COUNT] [--latest] [--oldest] [-w SUBSTR]
                 [-m NAME] [--since DATE] [--before DATE] [-s QUERY] [-t NAME]
                 [-l NAME] [--json] [--var KEY=VALUE]
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
  -l, --label NAME      Filter by label name
  --json                Output as JSON array
  --var KEY=VALUE       Substitute $KEY with VALUE in SQL (for 'sql'
                        subcommand)

examples:
  tbd query                         # list recent conversations
  tbd query -w myproject            # filter by workspace
  tbd query -s "error handling"     # FTS5 search
  tbd query <id>                    # show conversation detail
  tbd query sql                     # list available .sql files
  tbd query sql cost                # run the 'cost' query
  tbd query sql cost --var ws=proj  # run with variable substitution
```

## backfill

```
usage: tbd backfill [-h]

options:
  -h, --help  show this help message and exit
```

## path

```
usage: tbd path [-h]

options:
  -h, --help  show this help message and exit
```
