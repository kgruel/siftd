Test siftd doctor commands

Setup an isolated test database:

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/test.db ingest > /dev/null 2>&1

Doctor list shows available checks:

  $ siftd --db $PRYSK_TEMP/test.db doctor list | grep "Available checks:"
  Available checks:

  $ siftd --db $PRYSK_TEMP/test.db doctor list | grep -c "ingest-pending"
  1

Doctor runs without error on isolated DB (exit code 0):

  $ HOME=$PRYSK_TEMP XDG_DATA_HOME=$PRYSK_TEMP/data XDG_CONFIG_HOME=$PRYSK_TEMP/config siftd --db $PRYSK_TEMP/test.db doctor > /dev/null 2>&1; echo "exit code: $?"
  exit code: 0

Doctor JSON output is valid:

  $ HF_HUB_DISABLE_PROGRESS_BARS=1 siftd --db $PRYSK_TEMP/test.db doctor --json 2>/dev/null | python -m json.tool > /dev/null; echo "valid json: $?"
  valid json: 0

Doctor fix commands reference real subcommands (verified via Python):

  $ HF_HUB_DISABLE_PROGRESS_BARS=1 siftd --db $PRYSK_TEMP/test.db doctor --json 2>/dev/null | \
  > python -c "
  > import json, sys, subprocess
  > data = json.load(sys.stdin)
  > for finding in data.get('findings', []):
  >     fix_cmd = finding.get('fix_command', '')
  >     if fix_cmd and fix_cmd.startswith('siftd '):
  >         parts = fix_cmd.split()
  >         if len(parts) > 1:
  >             subcommand = parts[1]
  >             if subcommand.startswith('--'):
  >                 continue
  >             result = subprocess.run(['siftd', subcommand, '--help'],
  >                                     capture_output=True, text=True)
  >             if result.returncode != 0:
  >                 print(f'INVALID FIX COMMAND: {fix_cmd}')
  >                 sys.exit(1)
  > print('All fix commands are valid')
  > "
  All fix commands are valid
