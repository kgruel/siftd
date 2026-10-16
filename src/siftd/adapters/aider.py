"""Aider adapter for siftd.

Pure parser: reads .aider.chat.history.md files. :func:`read_aider` yields the
file's markdown constructs as units, :class:`AiderDecoder` turns each unit into
canonical records, and the shared assembler (:mod:`siftd.adapters.assembly`)
decides where they attach. Each session (delimited by ``# aider chat started
at``) becomes a separate Conversation.

Aider appends every session for a project to one history file, so this is a
session-strategy adapter: the source is a growing container of conversations,
not a single transcript. The last session in the file is the live one.

Discovery is opt-in via ``--path``. DEFAULT_LOCATIONS covers ``~/.aider``.
Chat history files are scattered across project directories; the user
supplies scan roots explicitly.

Aider is the one supported log format that timestamps in local wall time
with no offset, so this is the one adapter that has to resolve a zone
rather than pass a timestamp through.
"""

import hashlib
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from siftd.adapters.records import Ignored, PatchRec, PromptRec, Record, ResponseRec, Session, Unknown, UsageRec
from siftd.adapters.sdk import seek_last_lines, spells
from siftd.dateparse import local_to_utc
from siftd.domain import ContentBlock, Source, Usage

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 2
SUPPORT_TIER = "frozen"
NAME = "aider"
DEFAULT_LOCATIONS = ["~/.aider"]
DEDUP_STRATEGY = "session"  # one file accumulates every session for a project
PEEK_GLOB_PATTERNS = ["**/.aider.chat.history.md"]
PEEK_LAST_OF_MANY = True  # every session is appended to one file; the last is the live one
# Aider's session id is a digest of the file's path, so the id never appears in the path.
SESSION_ID_IN_PATH = False

# Harness metadata
HARNESS_SOURCE = "multi"  # aider supports multiple LLM providers
HARNESS_LOG_FORMAT = "markdown"
HARNESS_DISPLAY_NAME = "Aider"

# No structured tool names in aider's markdown format
TOOL_ALIASES: dict[str, str] = {}

# Regex for session headers: # aider chat started at 2025-07-15 14:32:01
_SESSION_RE = re.compile(r"^# aider chat started at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Regex for token lines: > Tokens: 4.5k sent, 1.2k received. Cost: $0.02
_COST_RE = re.compile(
    r"Tokens:\s*([\d.,]+[kKmM]?)\s*sent,\s*([\d.,]+[kKmM]?)\s*received\."
    r"(?:\s*Cost:\s*\$([\d.,]+))?"
)


@dataclass(frozen=True)
class _Unit:
    """One markdown construct of a history file.

    ``kind`` is one of the kinds the decoder table names. ``text`` is the
    construct's own content (prefixes removed, outer blank lines stripped).
    ``stamp`` and ``next_stamp`` belong to a session unit: its header's local
    time, and the next header's, which is where the session ended.
    """

    kind: str
    text: str = ""
    line: int = 0
    stamp: str | None = None
    next_stamp: str | None = None


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for aider files.

    Args:
        locations: Directories to scan. Falls back to DEFAULT_LOCATIONS.
                   When provided via ``--path``, scans for chat history files.
    """
    for location in (locations or DEFAULT_LOCATIONS):
        base = Path(location).expanduser()
        if not base.exists():
            continue
        # analytics.jsonl skipped — ingestion deferred until schema is documented upstream
        for md_file in base.glob("**/.aider.chat.history.md"):
            yield Source(kind="file", location=md_file)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    return Path(source.location).name == ".aider.chat.history.md"


def read_aider(source: Source) -> Iterator[_Unit]:
    """Yield the file's markdown constructs as units, in file order.

    Blank text is no unit: a run of blank lines separates blocks and is not
    content. Text before the first header is one ``outside`` unit, so it is
    counted rather than discarded. A session's end is the next header's start,
    which only the whole file states, so the reader looks it up and hands it on.
    A file not named like a history file has no units.
    """
    if source.as_path.name != ".aider.chat.history.md":
        return
    text = source.as_path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    headers = [index for index, line in enumerate(lines) if _SESSION_RE.match(line)]
    if not headers:
        if text.strip():
            yield _Unit("outside", text=text.strip(), line=1)
        return
    before = "\n".join(lines[: headers[0]]).strip()
    if before:
        yield _Unit("outside", text=before, line=1)
    for number, first in enumerate(headers):
        stamp = _SESSION_RE.match(lines[first]).group(1)
        following = headers[number + 1] if number + 1 < len(headers) else None
        next_stamp = _SESSION_RE.match(lines[following]).group(1) if following is not None else None
        yield _Unit("session", line=first + 1, stamp=stamp, next_stamp=next_stamp)
        end = following if following is not None else len(lines)
        yield from _body_units(lines[first + 1 : end], first + 2)


def _body_units(lines: list[str], first: int) -> list[_Unit]:
    """The units of one session's body, classified by line prefix.

    ``#### `` lines are user text, ``> `` lines are aider's own output, and any
    other line is the model's reply. A run of one kind is one unit. A ``> ``
    line that is a token line is a unit of its own, so its counts are never
    mixed into the output text around it.
    """
    units: list[_Unit] = []
    kind: str | None = None
    buffer: list[str] = []
    start = first

    def close() -> None:
        if kind is None:
            return
        body = "\n".join(buffer).strip()
        if body:
            units.append(_Unit(kind, text=body, line=start))

    for offset, line in enumerate(lines):
        number = first + offset
        if line.startswith("#### "):
            role, piece = "user", line[5:]
        elif line.startswith("> "):
            piece = line[2:]
            if _token_counts(piece) is not None:
                close()
                kind, buffer = None, []
                units.append(_Unit("tokens", text=piece, line=number))
                continue
            role = "tool"
        else:
            role, piece = "assistant", line
        if role != kind:
            close()
            kind, buffer, start = role, [], number
        buffer.append(piece)
    close()
    return units


class AiderDecoder:
    """Turns each markdown construct of a history file into canonical records.

    Every unit kind the reader yields is classified here; a kind this table does
    not name is ``Unknown``. A decoder holds only decoding state: the session's
    turn (the reply that output and usage join), and how many times each start
    time has been seen, which numbers repeated sessions.

    | Unit | Becomes | Why |
    |---|---|---|
    | `session` | Session | A `# aider chat started at` header. Starts at the header's time, read as local and stored as UTC. Ends at the next header's time, and is open (`ended_at` None) for the last session, which aider is still appending to. `infer_end` is False: aider states no end, so ingest re-checks the session whenever the file grows. |
    | `user` | PromptRec | A run of `#### ` lines: what the human typed. Starts a new prompt, and ends the turn before it. |
    | `assistant` | ResponseRec | A run of lines that are neither `####` nor `>`: the model's reply. Starts a new turn. |
    | `tool` | ResponseRec, merged into the turn | A run of `> ` lines that are not a token line: aider's own output (applied edits, commits, prompts). It joins the turn it follows. Only with no reply before it in the prompt does it hold a response of its own. |
    | `tokens` | UsageRec on the turn, and PatchRec `approx_cost` when stated | `> Tokens: X sent, Y received. Cost: $Z`: one API call's usage. Usage sums across the lines of a turn; the stated cost is the last line's, as it always was. With no reply before it, the line holds an empty response for its usage. |
    | `outside` | Ignored (`outside:before-first-session`) | Text before the first header. No session holds it, so it is named, not stored. |

    Identity. The conversation is ``aider::<digest>::<header time>``. The digest
    is of the resolved absolute path, so one file reached by a relative and an
    absolute scan root gets one id. A file scanned under a different spelling
    before this rule names the old ids as superseded candidates. Ingest adopts a
    stored row under one only when the row's prompts are this file's prompts, so
    a candidate that names another file adopts nothing.
    A repeated header time takes ``#2``, ``#3``, ... in file order, as before.
    """

    def __init__(self, source: Source):
        current = _identity_path(source)
        self._digest = _digest(current)
        self._superseded_digests = _superseded_digests(source, current)
        self._workspace = str(Path(source.location).parent)
        self._stamps: Counter[str] = Counter()
        self._turns = 0
        self._turn: str | None = None

    def decode(self, unit: _Unit) -> list[Record]:
        kind = unit.kind
        if kind == "session":
            return [self._session(unit)]
        if kind == "user":
            self._turn = None
            return [PromptRec(at=None, content=[ContentBlock(block_type="text", content={"text": unit.text})])]
        if kind == "assistant":
            return self._new_turn([ContentBlock(block_type="text", content={"text": unit.text})])
        if kind == "tool":
            block = ContentBlock(block_type="tool_output", content={"text": unit.text})
            if self._turn is None:
                return self._new_turn([block])
            return [ResponseRec(at=None, key=self._turn, content=[block])]
        if kind == "tokens":
            return self._tokens(unit.text)
        if kind == "outside":
            return [Ignored(reason="outside:before-first-session")]
        return [Unknown(reason=f"unit:{kind}")]

    # -- decoding state --

    def _session(self, unit: _Unit) -> Session:
        stamp = unit.stamp or ""
        self._stamps[stamp] += 1
        ordinal = self._stamps[stamp]
        self._turn = None  # a turn never continues into the next session
        return Session(
            external_id=_external_id(self._digest, stamp, ordinal),
            started_at=local_to_utc(stamp),
            ended_at=local_to_utc(unit.next_stamp) if unit.next_stamp else None,
            infer_end=False,
            workspace_path=self._workspace,
            superseded_external_ids=tuple(_external_id(d, stamp, ordinal) for d in self._superseded_digests),
        )

    def _new_turn(self, content: list[ContentBlock]) -> list[Record]:
        self._turns += 1
        self._turn = f"turn:{self._turns}"
        return [ResponseRec(at=None, key=self._turn, content=content)]

    def _tokens(self, text: str) -> list[Record]:
        counts = _token_counts(text)
        if counts is None:  # the reader only yields token units it has counted
            return [Unknown(reason="tokens:unparsed")]
        sent, received, cost = counts
        out: list[Record] = []
        if self._turn is None:
            out.extend(self._new_turn([]))
        out.append(UsageRec(at=None, usage=Usage(input_tokens=sent, output_tokens=received), target_key=self._turn))
        if cost is not None:
            out.append(PatchRec(at=None, attributes={"approx_cost": cost}, target_key=self._turn))
        return out


def _identity_path(source: Source) -> str:
    """The path the conversation id is built from: the resolved absolute path.

    A source may state its own identity path verbatim (golden fixtures do, so
    their expected ids hold on any machine); that is the only override.
    """
    stated = source.metadata.get("identity_path")
    return str(stated) if stated else str(Path(source.location).resolve())


def _superseded_digests(source: Source, current: str) -> list[str]:
    """Digests of the spellings this file may have been stored under before identity was resolved.

    The previous release keyed a file on its path as the scan root spelled it:
    the path as given, which is absolute for an absolute root and relative to the
    working directory for a relative one. A relative spelling names the file only
    relative to the directory it was scanned from, and that directory is not
    recorded anywhere, so the candidates are: the spelling as scanned, the path
    relative to each ancestor of the file (a scan from inside the file's tree), and
    every spelling the bookkeeping recorded (``recorded_locations``, which covers a
    scan from a directory that is not an ancestor, such as ``../proj``). Generated
    and recorded spellings alike are kept only when they can name this file
    (``sdk.spells``): ``proj/...`` from a directory where it resolves to another
    project, or another project's absolute path, is that project's key, and a copy
    of its history holds its prompts, so containment would not refuse it. Ingest
    adopts a row under a candidate only when the row holds this file's own prompts
    (``orchestration._superseded_conversation``); a relative spelling that does not
    resolve and so may name another file too (a basename-only one names every
    project) is refused unless that file's history is a prefix of this one's.
    """
    spellings = [str(Path(source.location))]
    generated = [] if source.metadata.get("identity_path") else [
        os.path.relpath(current, ancestor) for ancestor in Path(current).parents
    ]
    recorded = source.metadata.get("recorded_locations", ())
    spellings.extend(s for s in (*generated, *recorded) if spells(s, current))
    digests = [_digest(s) for s in dict.fromkeys(spellings)]
    current_digest = _digest(current)
    return [d for d in dict.fromkeys(digests) if d != current_digest]


def _digest(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def _external_id(digest: str, stamp: str, ordinal: int) -> str:
    """Aider's header resolves to the second, so the second and later sessions opened in one second get ``#n``."""
    base = f"{NAME}::{digest}::{stamp}"
    return base if ordinal == 1 else f"{base}#{ordinal}"


def _token_counts(text: str) -> tuple[int, int, str | None] | None:
    """(sent, received, stated message cost) for a token line, or None when the text is not one."""
    match = _COST_RE.search(text)
    if match is None:
        return None
    sent = _parse_token_count(match.group(1))
    received = _parse_token_count(match.group(2))
    if sent is None or received is None:
        return None
    return sent, received, match.group(3)


def _parse_token_count(s: str) -> int | None:
    """Parse a token count string like '4.5k', '1.2k', '500' into an int."""
    s = s.strip().replace(",", "")
    multiplier = 1
    if s.lower().endswith("k"):
        multiplier = 1000
        s = s[:-1]
    elif s.lower().endswith("m"):
        multiplier = 1_000_000
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return None


def peek_tail(path: Path, lines: int = 20) -> Iterator[str]:
    """The last raw lines of the history file (markdown has no records to parse)."""
    yield from seek_last_lines(path, lines)


read = read_aider
decoder = AiderDecoder
