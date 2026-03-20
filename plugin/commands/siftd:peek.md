---
name: "siftd:peek"
description: "View live or recent sessions without touching the database"
argument-hint: '[session-id] [--last-response] [--full] [-w workspace]'
---

# /siftd:peek — Live session inspection

View active or recent sessions directly from log files (bypasses DB).

## Results

!`siftd peek $ARGUMENTS 2>&1`

## Next steps

- See full session: `siftd peek <id> --full`
- Get last response text: `siftd peek <id> --last-response`
- Tag current session: `/siftd:tag <tag>`
- Follow live: `siftd peek <id> --follow` (in terminal)
