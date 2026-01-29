#!/usr/bin/env python3
"""Find conversations about technology decisions.

A parameterized query pattern for finding past decision discussions.
Demonstrates how to encode a reusable search as a function.

Example:
    # Find why we chose postgres
    python find-decisions.py postgres -w myproject

    # Find any database decisions with verbose output
    python find-decisions.py database -w myproject -v

    # Compare decisions across workspaces
    python find-decisions.py redis -w projectA
    python find-decisions.py redis -w projectB
"""

import argparse
import sys

from siftd.api import get_conversation, hybrid_search


def find_tech_decisions(
    technology: str,
    *,
    workspace: str | None = None,
    limit: int = 5,
) -> list:
    """Find conversations about technology decisions.

    Searches for rationale discussions — "why we chose X", tradeoffs,
    alternatives considered.
    """
    query = f"why we chose {technology}, decision rationale tradeoffs alternatives"
    return hybrid_search(
        query,
        workspace=workspace,
        limit=limit,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Find conversations about technology decisions."
    )
    parser.add_argument("technology", help="Technology to search for (e.g., postgres, redis, jwt)")
    parser.add_argument("--workspace", "-w", help="Filter by workspace path substring")
    parser.add_argument("--limit", "-n", type=int, default=5, help="Max results (default: 5)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full excerpts")
    parser.add_argument("--drill", "-d", help="Drill into a specific conversation ID")
    args = parser.parse_args()

    if args.drill:
        # Drill-down mode: show conversation timeline
        detail = get_conversation(args.drill)
        if not detail:
            print(f"Conversation not found: {args.drill}", file=sys.stderr)
            return 1

        print(f"Conversation: {detail.id}")
        print(f"Workspace: {detail.workspace_path or '(root)'}")
        print(f"Started: {detail.started_at}")
        print(f"Tokens: {detail.total_input_tokens:,} in / {detail.total_output_tokens:,} out")
        print()

        for i, ex in enumerate(detail.exchanges, 1):
            print(f"--- Exchange {i} ---")
            if ex.prompt_text:
                print(f"User: {ex.prompt_text[:500]}{'...' if len(ex.prompt_text) > 500 else ''}")
            if ex.response_text:
                print(f"Assistant: {ex.response_text[:500]}{'...' if len(ex.response_text) > 500 else ''}")
            if ex.tool_calls:
                tools = ", ".join(f"{tc.tool_name} x{tc.count}" for tc in ex.tool_calls)
                print(f"Tools: {tools}")
            print()
        return 0

    # Search mode
    results = find_tech_decisions(
        args.technology,
        workspace=args.workspace,
        limit=args.limit,
    )

    if not results:
        print(f"No conversations found about '{args.technology}'.", file=sys.stderr)
        return 1

    print(f"Conversations about '{args.technology}' decisions:")
    print()

    for r in results:
        ws = r.workspace_path.split("/")[-1] if r.workspace_path else "(root)"
        date = r.started_at[:10] if r.started_at else "?"

        print(f"{r.conversation_id[:12]}  {r.score:.3f}  {date}  {ws}")
        if args.verbose:
            # Show excerpt with word wrapping
            excerpt = r.text[:400].replace("\n", " ")
            print(f"  {excerpt}...")
            print()

    if not args.verbose:
        print()
        print("Use -v for excerpts, or -d <id> to drill into a conversation.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
