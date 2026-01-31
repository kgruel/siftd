Test basic siftd CLI commands

Help exits with code 0:

  $ siftd --help > /dev/null; echo "exit code: $?"
  exit code: 0

  $ siftd --help | grep -c "Aggregate and query"
  1

Version exits with code 0:

  $ siftd --version
  siftd * (glob)

Status works with fresh database (use empty path to avoid discovering real files):

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/test.db ingest > /dev/null 2>&1
  $ siftd --db $PRYSK_TEMP/test.db status | grep "Database:"
  Database: */test.db (glob)

  $ siftd --db $PRYSK_TEMP/test.db status | grep "Conversations:"
  *Conversations:* (glob)

Doctor runs without error on fresh isolated database:

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/doctor.db ingest > /dev/null 2>&1
  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/doctor.db doctor > /dev/null 2>&1; echo "exit code: $?"
  exit code: 0
