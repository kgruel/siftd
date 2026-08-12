Test basic siftd CLI commands

Help exits with code 0:

  $ siftd --help > /dev/null; echo "exit code: $?"
  exit code: 0

  $ siftd --help | grep -c "Aggregate and query"
  1

Version exits with code 0:

  $ siftd --version
  siftd * (glob)
  python * (glob)

Stats works with fresh database (use empty path to avoid discovering real files):

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/test.db ingest > /dev/null 2>&1
  $ siftd --db $PRYSK_TEMP/test.db db stats | grep "Database"
  *Database*test.db (glob)

  $ siftd --db $PRYSK_TEMP/test.db db stats | grep "Conversations"
  *Conversations* (glob)

Doctor runs without error on fresh isolated database.
Doctor's report is captured rather than discarded, and echoed only when the
exit code is not 0 — a failure here is otherwise indistinguishable from any
other, and the findings that explain it are exactly what gets thrown away:

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/doctor.db ingest > /dev/null 2>&1
  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/doctor.db doctor > $PRYSK_TEMP/doctor.out 2>&1; rc=$?; [ $rc -eq 0 ] || cat $PRYSK_TEMP/doctor.out; echo "exit code: $rc"
  exit code: 0
