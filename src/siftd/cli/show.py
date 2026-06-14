"""CLI handler for `show` — read one conversation (or event) in detail.

`show <id>` is the canonical conversation reader. `query <id>` remains a working
alias that routes through the same `_dispatch_detail` handler. That handler (and
the conversation/event detail renderers) still live in `query.py` to preserve
their established test surface; `show` is a thin, discoverable front-end over it
so the detail job has its own verb instead of riding query's magic positional.

Part of the CLI UX audit read-surface redesign (read-surface slice 2).
"""

import argparse


def cmd_show(args) -> int:
    """Show detail for one conversation or event."""
    from siftd.cli._common import apply_config_defaults
    from siftd.cli.query import _dispatch_detail
    from siftd.config import get_query_defaults

    # Honor the same configured fidelity defaults query <id> applies, so the
    # alias and the canonical verb render identically.
    apply_config_defaults(
        args,
        lambda: {k: v for k, v in get_query_defaults().items() if k in {"chars", "tool_chars"}},
    )
    return _dispatch_detail(args)


def build_show_parser(subparsers) -> None:
    """Add the 'show' subparser."""
    from siftd.cli._common import add_anchor_window_args, add_fidelity_args, add_output_args

    p = subparsers.add_parser(
        "show",
        help="Read one conversation (or event) in detail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Read a single conversation (or event) by ID, with navigation.

IDs may be any unambiguous prefix — the 12-char display IDs from `siftd query`
and `siftd search` resolve directly. Use `siftd query` to browse by metadata or
`siftd search <text>` to find conversations by content, then `show` to read one.

Navigation: --exchanges and --turns require an anchor flag. No anchor shows the
whole conversation.

examples:
  siftd show 01HX4G7K                              # full conversation
  siftd show 01HX4G7K --summary                    # metadata only, no turns
  siftd show 01HX4G7K --from-start --exchanges 3   # first 3 turns
  siftd show 01HX4G7K --from-end --exchanges 5     # last 5 turns
  siftd show 01HX4G7K --at-turn 4                  # show only turn 4
  siftd show 01HX4G7K --at-turn 4 --turns=-1:+2    # turns 3-6 (relative to turn 4)
  siftd show 01HX4G7K --around "error" --turns=-2:+2   # context around a phrase
  siftd show 01HX4G7K --brief                      # compact view (80 char truncation)
  siftd show 01HX4G7K --full                       # full text, no truncation""",
    )
    p.add_argument("conversation_id", help="Conversation or event ID (any unambiguous prefix)")
    add_output_args(p, json=True)
    add_fidelity_args(p, full=True, brief=True, chars=True, thinking=True, tools=True, tool_chars=True)
    add_anchor_window_args(p)
    detail_group = p.add_argument_group("detail view")
    detail_group.add_argument(
        "--summary", action="store_true", help="Summary only (metadata, no turns)"
    )
    detail_group.add_argument(
        "--neighbors", action="store_true",
        help="Include prev_event_id/next_event_id in event detail output",
    )
    p.set_defaults(func=cmd_show)
