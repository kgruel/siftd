"""Canonical records: what a decoder makes of one unit of a tool's log.

An adapter's only tool-specific job is decoding. Its reader yields raw units (a
JSONL line, a JSON message, a database row) and its decoder turns each unit into
zero or more of the records below. Everything after that is shared: the
assembler in :mod:`siftd.adapters.assembly` decides where each record attaches,
merges records that describe one event, pairs tool results with their calls,
and gives every record exactly one :class:`Disposition`, which the ledger
counts. Nothing is dropped without a name.

A decoder never decides structure. It does not look for "the current prompt",
does not synthesize a prompt for a response that arrives first, and does not
skip a unit it doesn't understand: it returns :class:`Unknown` (or nothing,
which the framework turns into ``Unknown``), and an intentional skip is
:class:`Ignored` with a reason.

Keys. ``key`` on a prompt or response is the source's own message identity
(Claude Code's ``message.id``): records sharing a key within a conversation are
one event, so the second and later ones MERGE into the first. ``parent_key`` /
``target_key`` point a tool call, usage delta or patch at the response with that
key; without one they attach to the most recent response.

Usage comes in two shapes, and they combine differently:

- ``ResponseRec.usage`` is a **snapshot** of that message's total. Repeated on
  every streamed chunk of one message, so merging keeps the last snapshot.
- :class:`UsageRec` is an **increment** (one API call's usage). Increments sum.

``usage_details`` holds token counts that have no first-class column (cache
reads and writes, reasoning tokens). They are stored as response attributes,
which is where the rollups read them, and follow the same snapshot/increment
rule as the usage they travel with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from siftd.domain import ContentBlock, Usage


class Disposition(StrEnum):
    """What became of one record. Every record gets exactly one."""

    EVENT = "event"  # became a new prompt, response or tool call
    MERGE = "merge"  # folded into an existing event with the same key
    PATCH = "patch"  # changed an existing event: a tool result, usage, late attributes
    LINEAGE = "lineage"  # named the conversation's parent
    META = "meta"  # a conversation-level fact: identity, workspace, model context
    IGNORED = "ignored"  # deliberately not stored; carries a reason
    ORPHAN = "orphan"  # could not be placed, e.g. a result whose call never appears
    UNKNOWN = "unknown"  # the decoder did not recognize the unit


@dataclass(frozen=True, kw_only=True)
class Session:
    """Opens (or names) a conversation.

    Content that arrives before any ``Session`` belongs to the conversation the
    first ``Session`` names. A ``Session`` with a different ``external_id`` from
    the current one closes it and opens the next, which is how one source holds
    several conversations. Fields left ``None`` fall back to what the records
    imply (``started_at``/``ended_at`` from event timestamps).
    """

    external_id: str
    started_at: str | None = None
    ended_at: str | None = None
    # False when the source cannot say when a session ended (aider stamps only
    # session starts): ended_at then stays None instead of being inferred from
    # event times, which is what tells ingest to re-check the session whenever
    # its source changes. Any Session stating False wins.
    infer_end: bool = True
    workspace_path: str | None = None
    branch: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Meta:
    """A conversation-level fact that arrives mid-stream.

    ``workspace_path`` and ``branch`` fill the conversation's value only if it
    has none (first wins: a session that changes directory keeps where it
    started). ``model``/``provider`` are context: they apply to later responses
    that carry none, and a later ``Meta`` replaces them. ``attributes`` update
    the conversation's attributes (later wins).
    """

    at: str | None = None
    workspace_path: str | None = None
    branch: str | None = None
    model: str | None = None
    provider: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class PromptRec:
    """A user turn. Opens a new prompt unless its ``key`` was already seen."""

    at: str | None
    content: list[ContentBlock] = field(default_factory=list)
    external_id: str | None = None
    key: str | None = None


@dataclass(frozen=True, kw_only=True)
class ResponseRec:
    """A model message, or one streamed chunk of one (chunks share ``key``)."""

    at: str | None
    content: list[ContentBlock] = field(default_factory=list)
    external_id: str | None = None
    key: str | None = None
    model: str | None = None
    provider: str | None = None
    usage: Usage | None = None
    usage_details: dict[str, int] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolCallRec:
    """A tool invocation. Attaches to the response ``parent_key`` names, else the latest."""

    at: str | None
    name: str
    input: dict
    call_id: str | None = None
    parent_key: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolResultRec:
    """A tool's result, paired to its call by ``call_id`` wherever the call appears."""

    at: str | None
    call_id: str
    result: dict
    status: str = "success"
    attributes: dict[str, str] = field(default_factory=dict)
    # A non-final result (a long-running command's interim output) updates the
    # call but leaves it open for the result that completes it.
    final: bool = True


@dataclass(frozen=True, kw_only=True)
class UsageRec:
    """A usage increment (one API call). Sums onto ``target_key``'s response, else the latest."""

    at: str | None
    usage: Usage
    usage_details: dict[str, int] = field(default_factory=dict)
    target_key: str | None = None


@dataclass(frozen=True, kw_only=True)
class PatchRec:
    """Late response attributes (a turn's duration, a stop reason). Later wins."""

    at: str | None
    attributes: dict[str, str]
    target_key: str | None = None


@dataclass(frozen=True, kw_only=True)
class LineageRec:
    """Names the conversation this one continues.

    ``kind`` is one of ``subagent``, ``fork``, ``resume``, ``branch``.
    ``parent`` is the parent conversation's identity as the source spells it;
    ``anchor`` is the parent record the child continues from, when known.
    A conversation has one parent per relation: where it ran (``subagent``) and
    what it continues (the other kinds), so a forked sub-agent states both.
    """

    at: str | None
    kind: str
    parent: str
    anchor: str | None = None


@dataclass(frozen=True, kw_only=True)
class Ignored:
    """A unit deliberately not stored. ``reason`` names why, and is counted."""

    reason: str


@dataclass(frozen=True, kw_only=True)
class Unknown:
    """A unit the decoder did not recognize. Counted, never silent."""

    reason: str = "unrecognized"


Record = (
    Session
    | Meta
    | PromptRec
    | ResponseRec
    | ToolCallRec
    | ToolResultRec
    | UsageRec
    | PatchRec
    | LineageRec
    | Ignored
    | Unknown
)


@dataclass(frozen=True)
class Malformed:
    """A raw unit the reader could not even parse (a truncated JSONL line).

    Readers yield this instead of skipping; the framework records it as
    :class:`Unknown` without calling the decoder.
    """

    line: int
    error: str
