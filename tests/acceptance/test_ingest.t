Test siftd ingest command

Ingest creates database when it doesn't exist:

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/new.db ingest 2>&1 | grep "Creating database"
  Creating database: */new.db (glob)
  $ test -f $PRYSK_TEMP/new.db && echo "database exists"
  database exists

Ingest with --rebuild-fts flag works:

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/fts.db ingest > /dev/null 2>&1
  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/fts.db ingest --rebuild-fts 2>&1 | grep "Rebuilding FTS"
  Rebuilding FTS index...

Ingest help shows --rebuild-fts option:

  $ siftd ingest --help | grep "^\s*--rebuild-fts"
    --rebuild-fts       Rebuild FTS index from existing data (skips ingestion)
