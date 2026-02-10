---
name: "siftd:search"
description: "Run a siftd search and show results directly"
argument-hint: '"query" [-w workspace] [--thread] [--genesis] [--recent]'
---

# /siftd:search — Direct search

Runs siftd search and shows results without agent interpretation.

## Current workspace
!`basename "$PWD"`

## Results

!`siftd search $ARGUMENTS 2>&1`

## Next steps

- Drill into a result: `siftd query <id>`
- Refine with workspace: `siftd search -w <workspace> "query" --thread`
- Tag a useful find: `siftd tag <id> research:<topic>`
- See surrounding context: `siftd search "query" --context 2`
