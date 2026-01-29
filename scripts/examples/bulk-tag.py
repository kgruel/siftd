#!/usr/bin/env python3
"""Bulk-tag conversations matching a semantic query.

This is the canonical search -> threshold -> tag pattern. Use it when you've
discovered a useful semantic cluster via CLI exploration and want to encode
that cluster as a tag for efficient future retrieval.

Example:
    # Tag all conversations about vocabulary-as-architecture
    python bulk-tag.py "vocabulary as architecture, naming carries structural insight" \
        --tag principle:vocabulary --threshold 0.65

    # Dry-run first to see what would be tagged
    python bulk-tag.py "error handling patterns" --tag pattern:errors --dry-run
"""

import argparse
import sys

from siftd.api import aggregate_by_conversation, hybrid_search
from siftd.paths import db_path
from siftd.storage.sqlite import open_database
from siftd.storage.tags import apply_tag, get_or_create_tag


def main():
    parser = argparse.ArgumentParser(
        description="Bulk-tag conversations matching a semantic query."
    )
    parser.add_argument("query", help="Semantic search query")
    parser.add_argument("--tag", "-t", required=True, help="Tag name to apply")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.6,
        help="Minimum score to tag (default: 0.6)",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=100,
        help="Max conversations to consider (default: 100)",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        help="Filter by workspace path substring",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be tagged without applying",
    )
    args = parser.parse_args()

    # Search
    results = hybrid_search(
        args.query,
        workspace=args.workspace,
        limit=args.limit * 3,  # fetch more chunks for better aggregation
    )
    if not results:
        print("No results found.", file=sys.stderr)
        return 1

    # Aggregate to conversation level
    convs = aggregate_by_conversation(results, limit=args.limit)

    # Filter by threshold
    matches = [c for c in convs if c.max_score >= args.threshold]
    if not matches:
        print(f"No conversations above threshold {args.threshold}.", file=sys.stderr)
        return 1

    print(f"Found {len(matches)} conversations above threshold {args.threshold}:")
    print()

    if args.dry_run:
        for conv in matches:
            ws = conv.workspace_path.split("/")[-1] if conv.workspace_path else "(root)"
            print(f"  {conv.conversation_id[:12]}  {conv.max_score:.3f}  {ws}")
        print()
        print(f"Would tag {len(matches)} conversations with '{args.tag}'")
        print("Run without --dry-run to apply.")
        return 0

    # Apply tags
    conn = open_database(db_path())
    tag_id = get_or_create_tag(conn, args.tag)

    tagged = 0
    skipped = 0
    for conv in matches:
        result = apply_tag(conn, "conversation", conv.conversation_id, tag_id)
        ws = conv.workspace_path.split("/")[-1] if conv.workspace_path else "(root)"
        if result:
            tagged += 1
            print(f"  + {conv.conversation_id[:12]}  {conv.max_score:.3f}  {ws}")
        else:
            skipped += 1
            print(f"  = {conv.conversation_id[:12]}  {conv.max_score:.3f}  {ws}  (already tagged)")

    conn.commit()
    conn.close()

    print()
    print(f"Tagged {tagged} conversations with '{args.tag}' ({skipped} already had tag)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
