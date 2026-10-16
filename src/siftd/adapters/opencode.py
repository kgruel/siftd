"""OpenCode adapter for siftd.

Pure decoder: reads OpenCode's SQLite database row by row and yields Conversation
domain objects. No storage coupling. The adapter only decodes:
:class:`OpencodeDecoder` turns each row into canonical records, and the shared
assembler (:mod:`siftd.adapters.assembly`) decides where they attach.

One database holds many sessions, so this is a container source
(``DEDUP_STRATEGY = "session"``): each session row is one conversation. Peek is
deferred for this reason (see the end of :class:`OpencodeDecoder`'s docstring).
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from siftd.adapters.records import (
    CONTENT_REVISION_ATTRIBUTE,
    Ignored,
    LineageRec,
    PromptRec,
    Record,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
)
from siftd.adapters.sdk import AdapterParseError, open_external_db
from siftd.domain import ContentBlock, Source, Usage
from siftd.safecall import epoch_ms_to_iso

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 2
SUPPORT_TIER = "contrib"
NAME = "opencode"
DEFAULT_LOCATIONS = ["~/.local/share/opencode"]
DEDUP_STRATEGY = "session"  # multiple conversations from one DB
# Peek is deferred for this adapter: one database holds every session, and peek
# shows one file as one conversation.
SUPPORTS_PEEK = False

# Harness metadata
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "sqlite"
HARNESS_DISPLAY_NAME = "OpenCode"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {}

# Part types that carry no conversation content. Each is Ignored by name; the
# docstring of OpencodeDecoder says why, and tests/adapters/test_opencode.py keeps
# that table and this set equal.
_IGNORED_PART_TYPES = frozenset({"step-start", "step-finish", "patch"})

# SQLite stores integers in 64 bits; a count outside that range cannot be written.
_SQLITE_INT_RANGE = range(-(2**63), 2**63)


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for OpenCode databases."""
    for location in locations or DEFAULT_LOCATIONS:
        base = Path(location).expanduser()
        if not base.exists():
            continue
        db_path = base / "opencode.db"
        if db_path.is_file():
            yield Source(kind="sqlite", location=db_path)


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "sqlite":
        return False
    path = Path(source.location)
    return path.name == "opencode.db"


def change_paths(source: Source) -> list[Path]:
    """The files whose change means this database may have changed.

    A WAL-mode database keeps recent commits in ``opencode.db-wal`` until a
    checkpoint moves them into the main file, so the database file alone is not
    a change signal: a commit that is still in the log would be invisible.
    """
    db = source.as_path
    return [db, db.with_name(db.name + "-wal")]


def read_rows(source: Source) -> Iterator[dict]:
    """Yield the rows of an OpenCode database as units, conversation by conversation.

    A session row comes first, then each of its messages, each followed by its
    parts. Two facts span rows, so they are computed here once per session: a
    digest of every row's content (the session's content revision), and whether a
    message carries any text (a user message with none falls back to its summary
    title, as it always did).
    """
    path = source.as_path
    if not path.exists():
        raise AdapterParseError(f"OpenCode source {path} does not exist")
    if not path.is_file():
        raise AdapterParseError(f"OpenCode source {path} is not a file")
    try:
        conn = open_external_db(path)
    except sqlite3.Error as e:
        raise AdapterParseError(f"OpenCode source {path} could not be opened as SQLite: {e}") from e
    try:
        yield from _rows(conn, path)
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, path: Path) -> Iterator[dict]:
    # ``parent_id`` is read only where the schema has it: databases from before
    # the child-session column existed still parse.
    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
    if not session_columns:
        raise AdapterParseError(f"OpenCode source {path} is missing the session table")
    parent = "parent_id" if "parent_id" in session_columns else "NULL"
    try:
        sessions = conn.execute(
            f"SELECT id, directory, {parent} AS parent_id, time_created, time_updated "
            "FROM session ORDER BY time_created"
        ).fetchall()
    except sqlite3.OperationalError as e:
        raise AdapterParseError(f"OpenCode source {path} has no readable session table: {e}") from e

    for session in sessions:
        session_id = session["id"]
        try:
            messages = conn.execute(
                "SELECT id, session_id, data, time_created, time_updated FROM message "
                "WHERE session_id = ? ORDER BY time_created, id",
                (session_id,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            raise AdapterParseError(f"OpenCode source {path} has no readable message table: {e}") from e
        parts = _parts_by_message(conn, session_id, path)

        yield {
            "table": "session",
            "row": dict(session),
            "content_revision": _revision(session, messages, parts),
        }
        placed: set[str] = set()
        for message in messages:
            placed.add(message["id"])
            rows = parts.get(message["id"], [])
            yield {
                "table": "message",
                "row": dict(message),
                "has_content": any(_has_content(p["data"]) for p in rows),
            }
            for part in rows:
                yield {"table": "part", "row": dict(part)}
        # Parts whose message is not in this session are read too, so the decoder
        # counts them as Unknown rather than the reader dropping them.
        for message_id, rows in parts.items():
            if message_id not in placed:
                for part in rows:
                    yield {"table": "part", "row": dict(part)}


def _revision(session: sqlite3.Row, messages: list[sqlite3.Row], parts: dict[str, list[sqlite3.Row]]) -> str:
    """A digest of every row a session was read from, in the order the reader yields them.

    Every column is covered, so an edit that moves no timestamp (same millisecond,
    or a message stamped earlier than the session's end) still changes it.
    """
    digest = hashlib.sha256()
    rows = [("session", session), *(("message", m) for m in messages)]
    rows += [("part", p) for message_parts in parts.values() for p in message_parts]
    for table, row in rows:
        digest.update(json.dumps([table, dict(row)], sort_keys=True, default=repr).encode())
    return digest.hexdigest()


def _parts_by_message(conn: sqlite3.Connection, session_id: str, path: Path) -> dict[str, list[sqlite3.Row]]:
    try:
        rows = conn.execute(
            "SELECT id, message_id, session_id, data, time_created, time_updated FROM part "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        raise AdapterParseError(f"OpenCode source {path} has no readable part table: {e}") from e
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["message_id"], []).append(row)
    return grouped


class OpencodeDecoder:
    """Turns each OpenCode database row into canonical records.

    Every unit is a session, message or part row (see :func:`read_rows`), and
    every raw type the corpus holds is classified in the table below; a type
    the table does not name is ``Unknown``. A decoder holds only decoding state:
    the session whose rows are arriving, and the role each message decoded to,
    so a part can be placed on the right side of the conversation.

    | Unit | Becomes | Why |
    |---|---|---|
    | `session` | Session, plus LineageRec (subagent) when `parent_id` is set | One conversation. A session that names a parent ran inside it. |
    | `message:user` | PromptRec | A human turn. Its text arrives as its parts. With no text part, its `summary.title` is the content, as before. |
    | `message:assistant` | ResponseRec | A model message, keyed by its id. Usage is a snapshot of its tokens; `cost` and the model are attributes and context. |
    | `message` (other role) | Unknown | Not a role the adapter knows. |
    | `part:text` | text block on the message's prompt or response; an empty one is Ignored (`part:text:empty`) | Merges into that message by its key. |
    | `part:reasoning` | thinking block on the message's prompt or response; an empty one is Ignored (`part:reasoning:empty`) | Merges into that message. |
    | `part:tool` | ResponseRec (`tool_use` block), ToolCallRec, and a ToolResultRec once completed or errored | A call and its result are one part. A running call with output carries an interim, non-final result. |
    | `part:step-start` | Ignored | Step boundary; no content. |
    | `part:step-finish` | Ignored | Step boundary; its tokens and cost are the message's, already read. |
    | `part:patch` | Ignored | A snapshot of files changed by a step; not conversation content. |
    | any other part type | Unknown | Not a part type the adapter knows. |

    A malformed field never loses its unit. Token fields that are not counts, a
    tool with no state, and a row whose JSON does not parse are each named
    ``Unknown``; the rest of the unit still stores. A part whose message did not
    decode, or whose session did not open, is ``Unknown`` for the same reason:
    its message is the only place it can attach.

    Identity. ``opencode::<session id>`` names the conversation; messages and
    parts keep their own ids as their keys. A session with no usable id or start
    time is ``Unknown``, and so is everything in it.

    Time. A session's end is its own update time, as it has always been. A row
    changed after that moment does not move the end; it is still a change, which
    the revision catches.

    Revision. The session carries a content revision (``content.revision``), a
    digest of all its rows. Ingest replaces a stored session whose revision differs
    when both copies were read from the same database, which catches what the end
    time cannot: a message stamped before the session's last update, a message
    updated after it, or an edit that moves no time. A copy from another database
    competes on the end time alone; ingest names the database, not the adapter
    (see ``ingestion/orchestration.py``).

    Peek. Deferred: peek reads one conversation per file, and a database holds one
    per session. The adapter declares ``SUPPORTS_PEEK = False`` until a container-aware peek exists.
    """

    def __init__(self) -> None:
        self._session: str | None = None
        # message id -> "user" or "assistant" for the current session; None when
        # the message did not decode, so its parts are Unknown rather than guessed.
        self._roles: dict[str, str | None] = {}

    def decode(self, unit: dict) -> list[Record]:
        table = unit.get("table")
        if table == "session":
            return self._session_row(unit)
        if table == "message":
            return self._message(unit)
        if table == "part":
            return self._part(unit)
        return [Unknown(reason=f"table:{table}")]

    # -- session --

    def _session_row(self, unit: dict) -> list[Record]:
        row = unit["row"]
        session_id = row.get("id")
        started = _iso(row.get("time_created"))
        if not isinstance(session_id, str) or not session_id or started is None:
            self._session = None
            self._roles = {}
            return [Unknown(reason="session:no-id-or-time")]
        directory = row.get("directory")
        revision = unit.get("content_revision")
        self._session = session_id
        self._roles = {}
        records: list[Record] = [
            Session(
                external_id=f"{NAME}::{session_id}",
                started_at=started,
                ended_at=_iso(row.get("time_updated")),
                workspace_path=directory if isinstance(directory, str) else None,
                attributes={CONTENT_REVISION_ATTRIBUTE: revision} if isinstance(revision, str) else {},
            )
        ]
        parent = row.get("parent_id")
        if isinstance(parent, str) and parent:
            records.append(LineageRec(at=None, kind="subagent", parent=f"{NAME}::{parent}"))
        return records

    # -- message --

    def _message(self, unit: dict) -> list[Record]:
        row = unit["row"]
        message_id = row.get("id")
        if self._session is None or row.get("session_id") != self._session:
            return [Unknown(reason="message:no-session")]
        if not isinstance(message_id, str) or not message_id:
            return [Unknown(reason="message:no-id")]
        data = _json_object(row.get("data"))
        if data is None:
            self._roles[message_id] = None
            return [Unknown(reason="message:data")]
        role = data.get("role")
        at = _iso(row.get("time_created"))
        if role == "user":
            self._roles[message_id] = "user"
            content = [] if unit.get("has_content") else _title_block(data)
            return [PromptRec(at=at, key=message_id, external_id=f"{NAME}::{message_id}", content=content)]
        if role == "assistant":
            self._roles[message_id] = "assistant"
            return _response(message_id, at, data)
        self._roles[message_id] = None
        return [Unknown(reason=f"message:role:{role}")]

    # -- part --

    def _part(self, unit: dict) -> list[Record]:
        row = unit["row"]
        if self._session is None or row.get("session_id") != self._session:
            return [Unknown(reason="part:no-session")]
        message_id = row.get("message_id")
        role = self._roles.get(message_id) if isinstance(message_id, str) else None
        if role is None:
            return [Unknown(reason="part:no-message")]
        data = _json_object(row.get("data"))
        if data is None:
            return [Unknown(reason="part:data")]
        # Only a string names a part type. Anything else (a list, an object) is
        # unrecognized, and must not reach the set membership test below.
        kind = data.get("type")
        if not isinstance(kind, str):
            return [Unknown(reason="part:no-type")]
        at = _iso(row.get("time_created"))
        if kind in ("text", "reasoning"):
            return _text(kind, message_id, role, at, data)
        if kind == "tool":
            return _tool(message_id, role, at, data)
        if kind in _IGNORED_PART_TYPES:
            return [Ignored(reason=f"part:{kind}")]
        return [Unknown(reason=f"part:{kind}")]


def _response(message_id: str, at: str | None, data: dict) -> list[Record]:
    bad: list[str] = []
    model = data.get("modelID")
    if isinstance(model, str):
        model = model or None
    elif model is not None:
        bad.append("message:modelID")
        model = None
    attributes: dict[str, str] = {}
    cost = data.get("cost")
    if cost is not None:
        attributes["cost"] = str(cost)
    usage, details, token_bad = _tokens(data.get("tokens"))
    record = ResponseRec(
        at=at,
        key=message_id,
        external_id=f"{NAME}::{message_id}",
        model=model,
        usage=usage,
        usage_details=details,
        attributes=attributes,
    )
    return [record, *(Unknown(reason=name) for name in bad + token_bad)]


def _tokens(raw) -> tuple[Usage | None, dict[str, int], list[str]]:
    """A message's token counts, and the names of the fields that were not counts.

    A falsy ``tokens`` states nothing. A count is an integer SQLite can store; any
    other value is named in the returned list and left out, so the message's text
    still stores.
    """
    if not raw:
        return None, {}, []
    if not isinstance(raw, dict):
        return None, {}, ["message:tokens"]
    bad: list[str] = []

    def count(value, name: str) -> int | None:
        if value is None:
            return None
        if _ms(value) is None or value not in _SQLITE_INT_RANGE:
            bad.append(name)
            return None
        return value

    details: dict[str, int] = {}
    reasoning = count(raw.get("reasoning"), "message:tokens.reasoning")
    if reasoning is not None:
        details["reasoning_output_tokens"] = reasoning
    cache = raw.get("cache")
    if isinstance(cache, dict):
        read = count(cache.get("read"), "message:tokens.cache.read")
        write = count(cache.get("write"), "message:tokens.cache.write")
        if read is not None:
            details["cache_read_input_tokens"] = read
        if write is not None:
            details["cache_creation_input_tokens"] = write
    elif cache:
        bad.append("message:tokens.cache")
    usage = Usage(
        input_tokens=count(raw.get("input"), "message:tokens.input"),
        output_tokens=count(raw.get("output"), "message:tokens.output"),
    )
    return usage, details, bad


def _text(kind: str, message_id: str, role: str, at: str | None, data: dict) -> list[Record]:
    text = data.get("text")
    if text is None or text == "":
        return [Ignored(reason=f"part:{kind}:empty")]
    if not isinstance(text, str):
        return [Unknown(reason=f"part:{kind}:text")]
    block = ContentBlock(block_type="text" if kind == "text" else "thinking", content={"text": text})
    if role == "user":
        return [PromptRec(at=at, key=message_id, content=[block])]
    return [ResponseRec(at=at, key=message_id, content=[block])]


def _tool(message_id: str, role: str, at: str | None, data: dict) -> list[Record]:
    if role != "assistant":
        return [Unknown(reason="part:tool:user-message")]
    state = data.get("state")
    if not isinstance(state, dict):
        return [Unknown(reason="part:tool:state")]
    call_id = _str_or_none(data.get("callID"))
    name = _str_or_none(data.get("tool")) or "unknown"
    raw_input = state.get("input", {})
    tool_input = raw_input if isinstance(raw_input, dict) else {"raw": str(raw_input)}
    records: list[Record] = [
        ResponseRec(
            at=at,
            key=message_id,
            content=[ContentBlock(block_type="tool_use", content={"id": call_id, "name": name})],
        ),
        ToolCallRec(at=at, name=name, input=tool_input, call_id=call_id, parent_key=message_id),
    ]
    status = state.get("status")
    output = state.get("output")
    if status == "completed":
        result_status, final = "success", True
    elif status == "error":
        result_status, final = "error", True
    elif output:
        # A call still running that has printed something: an interim result,
        # which updates the call and leaves it open.
        result_status, final = "pending", False
    else:
        return records
    if call_id is None:
        return [*records, Unknown(reason="part:tool:no-call-id")]
    time_data = state.get("time")
    ended = _iso(time_data.get("end")) if isinstance(time_data, dict) else None
    records.append(
        ToolResultRec(
            at=ended,
            call_id=call_id,
            result={"output": output} if output else {},
            status=result_status,
            final=final,
        )
    )
    return records


def _title_block(data: dict) -> list[ContentBlock]:
    summary = data.get("summary")
    title = summary.get("title") if isinstance(summary, dict) else None
    if isinstance(title, str) and title:
        return [ContentBlock(block_type="text", content={"text": title})]
    return []


def _ms(value) -> int | None:
    """An epoch-milliseconds value, or None when it is not an integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _iso(value) -> str | None:
    ms = _ms(value)
    return None if ms is None else epoch_ms_to_iso(ms)


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_object(raw) -> dict | None:
    """The JSON object a row's ``data`` column holds, or None if it holds anything else."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _has_content(raw) -> bool:
    data = _json_object(raw)
    return (
        data is not None
        and data.get("type") in ("text", "reasoning")
        and isinstance(data.get("text"), str)
        and bool(data["text"])
    )


read = read_rows


def decoder(source: Source) -> OpencodeDecoder:
    """A decoder is made per source; opencode's rows state everything it needs, so this one ignores it."""
    return OpencodeDecoder()
