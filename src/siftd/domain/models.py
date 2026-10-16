"""Domain models for siftd.

Nested structure: Conversation → Prompt → Response → ToolCall
These are pure data objects, decoupled from storage.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ContentBlock:
    """A content block within a prompt or response."""
    block_type: str
    content: dict  # raw dict, storage layer serializes to JSON


@dataclass
class Usage:
    """Token usage statistics."""
    input_tokens: int | None = None
    output_tokens: int | None = None


#: Response attribute listing the external ids of the later chunks merged into
#: it (a JSON list). A merged response keeps its first chunk's id, so a tag a
#: person put on a later chunk finds its event again through this.
MERGED_EXTERNAL_IDS_ATTRIBUTE = "merged_external_ids"

#: Tool-call attribute stating the rank of a call that shares its ``call_id``
#: with an earlier one (2 and up), whose ``external_id`` is ``<id>:<rank>``. It
#: marks that suffix as a distinct call's identity, not a legacy alias of the
#: bare id, so a tag on it follows only an exact match.
CALL_OCCURRENCE_ATTRIBUTE = "call_occurrence"


@dataclass
class ToolCall:
    """A tool invocation within a response."""
    tool_name: str
    input: dict
    result: dict | None = None
    status: str = "pending"
    external_id: str | None = None
    timestamp: str | None = None
    # Key/value attributes, persisted to the polymorphic `attributes` table
    # (mirrors Conversation.attributes / Response.attributes). Convention:
    # adapters whose tools have background/async semantics (a call starts,
    # and its real result/relationship arrives later via a separately
    # correlated event or poll) set attributes["background_task_id"] to the
    # same value on every ToolCall belonging to that operation — e.g. Claude
    # Code's Bash(run_in_background) + its later BashOutput polls, or
    # Antigravity's backgrounded RUN_COMMAND. See claude_code.py/
    # antigravity_cli.py for the two current examples.
    attributes: dict[str, str] = field(default_factory=dict)
    # Recorded position among the conversation's events; see Prompt.sequence.
    sequence: int | None = None


@dataclass
class Response:
    """A model response to a prompt."""
    timestamp: str
    content: list[ContentBlock] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    model: str | None = None
    provider: str | None = None
    external_id: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    sequence: int | None = None


@dataclass
class Prompt:
    """A user prompt within a conversation."""
    timestamp: str
    content: list[ContentBlock] = field(default_factory=list)
    responses: list[Response] = field(default_factory=list)
    external_id: str | None = None
    # Position in the order the conversation recorded its events, stored as
    # ``events.sequence``. Set by the assembler on every event; a version-1
    # adapter sets none, and storage numbers its tree walk instead.
    sequence: int | None = None


@dataclass
class Harness:
    """Metadata about the CLI harness that produced the conversation."""
    name: str
    source: str | None = None
    log_format: str | None = None
    display_name: str | None = None


#: Lineage kinds that say where a conversation ran (a subagent runs inside its
#: parent session). Every other kind says what it continues. A conversation has
#: at most one parent of each relation, so a forked subagent keeps both.
CONTAINMENT_KINDS = frozenset({"subagent"})


class _HasKind(Protocol):
    @property
    def kind(self) -> str: ...


def continued_link[L: _HasKind](links: Sequence[L]) -> L | None:
    """The link that says what a conversation continues, for a header; None with no links.

    The relation that says what it continues (fork, resume, branch) wins over
    the one that says where it ran, so a forked sub-agent reads as a fork of its
    parent's transcript. Stored edges and an assembled ``Lineage`` both qualify:
    anything with a ``kind``. One rule for the stored and the live read.
    """
    return next((link for link in links if link.kind not in CONTAINMENT_KINDS), links[0] if links else None)


@dataclass(frozen=True)
class Lineage:
    """A conversation this one belongs to or continues.

    ``kind`` is ``subagent``, ``fork``, ``resume`` or ``branch``. ``parent`` is
    the parent's ``external_id`` as the source spells it (not a stored id: the
    parent may be re-ingested under a new one, or never ingested at all).
    ``anchor`` is the parent record the child continues from, when known.
    """
    kind: str
    parent: str
    anchor: str | None = None


@dataclass
class Conversation:
    """A complete conversation from a harness.

    Top-level domain object representing an interaction session.
    """
    external_id: str
    harness: Harness
    started_at: str
    prompts: list[Prompt] = field(default_factory=list)
    # Keyword-only: v1 adapters construct Conversation positionally through
    # `prompts`, so a new field must not take a positional slot.
    # Responses that arrived before any prompt (a forked sub-agent's inherited
    # history, an unwritten opening turn). Stored with no parent event.
    preamble: list[Response] = field(default_factory=list, kw_only=True)
    workspace_path: str | None = None
    branch: str | None = None
    ended_at: str | None = None
    # Conversation-level key/value attributes, persisted to the polymorphic
    # `attributes` table (mirrors Response.attributes). Adapters populate this
    # with derived facts that have no first-class column — e.g. the Claude Code
    # sub-agent type/description read from the agent-<id>.meta.json sidecar.
    attributes: dict[str, str] = field(default_factory=dict)
    # external_ids this conversation was stored under before its adapter re-keyed
    # it (e.g. a sessionId shared by several chat files). Ingest adopts a stored
    # row under one of these only when the row holds this conversation's
    # transcript (ingestion/orchestration.py, _holds_transcript_of). A superseded
    # key alone never claims a row.
    superseded_external_ids: tuple[str, ...] = ()
    # external_ids this conversation was stored under that are another
    # conversation's own id too: a forked codex file a previous release named by
    # the parent id it restates. Containment cannot tell the fork's row from the
    # parent's (a fork holds its parent's prompts), so ingest adopts a row under
    # one only when the row is also a read of this source: it begins when this
    # conversation begins.
    inherited_external_ids: tuple[str, ...] = ()
    # The conversations this one belongs to and continues: at most one per
    # relation (where it ran, CONTAINMENT_KINDS; what it continues, the rest).
    # Persisted as conversation_links rows, resolved against
    # conversations.external_id at read time.
    lineage: tuple[Lineage, ...] = ()

    @property
    def turn_count(self) -> int:
        """Turns: one per prompt, plus the preamble turn when there is a preamble.

        The parsed-conversation form of the rule the stored forms follow
        (:func:`siftd.storage.turns.fold_turns` and the listing's ``turn_count``).
        """
        return len(self.prompts) + (1 if self.preamble else 0)

    def parent(self, kind: str) -> Lineage | None:
        """The lineage link of ``kind``, if this conversation has one."""
        return next((link for link in self.lineage if link.kind == kind), None)
