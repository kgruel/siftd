---
name: "siftd:query"
description: "List or drill into conversations directly"
argument-hint: '[conversation-id] or [-w workspace] [-n count] [--stats]'
---

# /siftd:query — Direct query

Lists conversations or drills into a specific one.

## Results

!`siftd query $ARGUMENTS 2>&1`

## Next steps

- Search within results: `siftd search -w <workspace> "query" --thread`
- Tag a conversation: `siftd tag <id> <tag>`
- Filter by tool usage: `siftd query --tool-tag shell:test`
- Export for review: `siftd export <id>`
