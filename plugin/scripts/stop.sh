#!/bin/bash
# On session exit, run a targeted ingest to apply any pending tags.
#
# Only ingests the claude_code adapter (~0.7s) — not a full ingest.
# Runs quietly; the session is ending so there's no one to show output to.

command -v siftd >/dev/null 2>&1 || exit 0

siftd ingest -a claude_code -q 2>/dev/null

exit 0
