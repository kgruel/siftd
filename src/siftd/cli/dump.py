"""CLI handler for 'siftd dump' — golden assembly files for differential testing."""

import argparse
import sys
from pathlib import Path


def cmd_dump(args) -> int:
    """Write the golden JSON of one source, or of every fixture source."""
    from siftd.api import dump_fixtures, dump_golden_source, golden_json
    from siftd.errors import UserInputError
    from siftd.output import status

    if args.golden_fixtures:
        if not args.out:
            raise UserInputError("--golden-fixtures needs --out <dir>")
        if args.adapter:
            raise UserInputError("--adapter applies to --golden, not --golden-fixtures")
        dumped = dump_fixtures(args.golden_fixtures, args.out)
        for item in dumped.files:
            print(f"{item.adapter}/{item.case}.json  {item.conversations} conversation(s)", file=sys.stderr)
        for name in dumped.empty:
            status.warning(f"adapter {name}: no fixture sources")
        for name, cases in sorted(dumped.undiscovered.items()):
            status.info(f"adapter {name}: discover() found nothing in {len(cases)} case(s); read the case input directly")
        print(f"wrote {len(dumped.files)} file(s) to {args.out}", file=sys.stderr)
        return 0

    document = dump_golden_source(args.golden, adapter=args.adapter, root=args.root)
    text = golden_json(document)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
    else:
        # Bytes, not the text stream: main() relaxes stdout to backslashreplace, which would
        # spell an astral character as \U0001f600, not valid JSON.
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            sys.stdout.write(text)
        else:
            sys.stdout.flush()
            buffer.write(text.encode("utf-8"))
            buffer.flush()
    return 0


def build_dump_parser(subparsers) -> None:
    """Add 'dump' subparser."""
    p_dump = subparsers.add_parser(
        "dump",
        help="Write golden assembly JSON for a source or a fixtures tree",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd dump --golden session.jsonl                  # golden JSON to stdout
  siftd dump --golden session.jsonl --out s.json     # ...or to a file
  siftd dump --golden-fixtures tests/fixtures --out tests/golden/assembly""",
    )
    what = p_dump.add_mutually_exclusive_group(required=True)
    what.add_argument(
        "--golden",
        metavar="SOURCE",
        help="Dump one source file (conversations and ledger) as golden JSON",
    )
    what.add_argument(
        "--golden-fixtures",
        metavar="DIR",
        help="Dump every fixture source under DIR (adapters/<adapter>/<case>/ and <adapter>_minimal.*)",
    )
    p_dump.add_argument(
        "--adapter",
        metavar="NAME",
        help="Built-in adapter to read SOURCE with (default: inferred from its path)",
    )
    p_dump.add_argument(
        "--root",
        metavar="DIR",
        help="With --golden: state the source relative to this fixtures root, as --golden-fixtures does",
    )
    p_dump.add_argument(
        "--out",
        metavar="PATH",
        help="Output file for --golden (default: stdout); output directory for --golden-fixtures",
    )
    p_dump.set_defaults(func=cmd_dump)
