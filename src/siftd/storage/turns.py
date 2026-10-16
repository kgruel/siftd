"""The one fold from stored events to turns, and the one turn ordinal.

A **turn** is an optional prompt plus the responses (and their tool calls) that
follow it until the next prompt. The assembler stores responses that precede
every prompt with no parent (a forked sub-agent's whole transcript, a leading
tool announcement); they form the **preamble turn**, which has no prompt and is
always first. Every read surface that groups events into turns, counts them, or
maps an event to a turn number goes through this module, so they cannot disagree.

Membership is the stored parent link, never position: a response belongs to its
parent prompt's turn, a tool call to its parent response's turn, and a response
with no resolvable prompt to the preamble. Order is recorded order
(:func:`~siftd.storage.sql_helpers.event_order`). Position cannot carry
membership: legacy rows with equal timestamps have no order to infer it from.

**Numbering.** The turn list is ``[preamble?] + prompts``, and a turn's
*ordinal* is its 0-based index in it. A conversation with no preamble numbers
exactly as it always did (ordinal = prompt position), which is what tags,
``--at-turn`` and search positions refer to. One with a preamble numbers the
preamble 0 and each prompt one higher, which matches how live peek already counts
it. Preamble conversations are rare, and no stored reference counts turns:
element tags are keyed by event id, not by position.

The 1-based *exchange number* (``exchange_index``, ``conv:exchange:N``, the turn
in ``siftd id``) is ``ordinal + 1``. ``conv:prompt:N`` is a different thing: the
Nth event of kind prompt, addressed by kind (see
:func:`~siftd.storage.events.get_event_position`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from siftd.storage.sql_helpers import event_order


@dataclass(frozen=True)
class TurnSpan:
    """Which events one turn holds. ``prompt_id`` is None for the preamble turn."""

    prompt_id: str | None
    response_ids: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()

    @property
    def is_preamble(self) -> bool:
        return self.prompt_id is None

    @property
    def event_ids(self) -> tuple[str, ...]:
        """Every event of the turn: prompt first, then responses, then tool calls."""
        head = () if self.prompt_id is None else (self.prompt_id,)
        return (*head, *self.response_ids, *self.tool_call_ids)


def fold_turns(
    prompt_ids: Sequence[str],
    responses: Iterable[tuple[str, str | None]],
    tool_calls: Iterable[tuple[str, str | None]] = (),
) -> list[TurnSpan]:
    """Group events into turns: the preamble (if any) first, then one per prompt.

    ``prompt_ids`` are in recorded order; ``responses`` and ``tool_calls`` are
    ``(event id, parent id)`` pairs, each in recorded order. The three kinds need
    no interleaving: membership comes from the parent link, so only the order
    within a kind matters. A response whose parent is not one of ``prompt_ids``
    (NULL, or not given) and a tool call whose parent is not one of ``responses``
    belong to the preamble turn, which therefore exists exactly when such an
    event does.
    """
    by_prompt: dict[str, tuple[list[str], list[str]]] = {pid: ([], []) for pid in prompt_ids}
    pre_responses: list[str] = []
    pre_calls: list[str] = []
    # response id -> the (responses, tool calls) lists of the turn that holds it
    home: dict[str, tuple[list[str], list[str]]] = {}
    preamble_home = (pre_responses, pre_calls)

    for response_id, parent_id in responses:
        turn = by_prompt.get(parent_id) if parent_id is not None else None
        turn = preamble_home if turn is None else turn
        turn[0].append(response_id)
        home[response_id] = turn
    for call_id, parent_id in tool_calls:
        turn = home.get(parent_id) if parent_id is not None else None
        (preamble_home if turn is None else turn)[1].append(call_id)

    spans: list[TurnSpan] = []
    if pre_responses or pre_calls:
        spans.append(TurnSpan(None, tuple(pre_responses), tuple(pre_calls)))
    for pid in prompt_ids:
        held_responses, held_calls = by_prompt[pid]
        spans.append(TurnSpan(pid, tuple(held_responses), tuple(held_calls)))
    return spans


def load_turns(conn: sqlite3.Connection, conversation_id: str) -> list[TurnSpan]:
    """The turns of one stored conversation, in recorded order."""
    rows = conn.execute(
        f"SELECT id, kind, parent_id FROM events WHERE conversation_id = ? ORDER BY {event_order()}",
        (conversation_id,),
    ).fetchall()
    prompts = [r["id"] for r in rows if r["kind"] == "prompt"]
    responses = [(r["id"], r["parent_id"]) for r in rows if r["kind"] == "response"]
    calls = [(r["id"], r["parent_id"]) for r in rows if r["kind"] == "tool_call"]
    return fold_turns(prompts, responses, calls)


def ordinals_by_event(turns: Sequence[TurnSpan]) -> dict[str, int]:
    """Every event id in ``turns`` mapped to the ordinal of the turn that holds it."""
    return {eid: ordinal for ordinal, turn in enumerate(turns) for eid in turn.event_ids}


def event_turn_ordinal(conn: sqlite3.Connection, event_id: str) -> int | None:
    """The 0-based ordinal of the turn holding ``event_id``, or None for an unknown event.

    A conversation is folded per call; callers that map many events of one
    conversation fold once and use :func:`ordinals_by_event`.
    """
    row = conn.execute("SELECT conversation_id FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    return ordinals_by_event(load_turns(conn, row["conversation_id"])).get(event_id)


def turn_at_exchange(
    conn: sqlite3.Connection,
    conversation_id: str,
    exchange_index: int | None,
) -> TurnSpan | None:
    """The turn with 1-based exchange number ``exchange_index``, or None if absent.

    None for a None index or one past the end. Raises ValueError for a
    non-positive index: the numbering is 1-based, so 0 is a caller bug, not an
    empty result. The turn may be the preamble (``prompt_id`` None); the caller
    decides what that means for what it is resolving.
    """
    if exchange_index is None:
        return None
    if exchange_index < 1:
        raise ValueError(f"exchange_index must be >= 1, got {exchange_index}")
    turns = load_turns(conn, conversation_id)
    return turns[exchange_index - 1] if exchange_index <= len(turns) else None


def exchange_number(conn: sqlite3.Connection, event_id: str) -> int | None:
    """The 1-based exchange number of the turn holding ``event_id``."""
    ordinal = event_turn_ordinal(conn, event_id)
    return None if ordinal is None else ordinal + 1


def conversation_ids_with_turns(conn: sqlite3.Connection) -> set[str]:
    """Ids of conversations that have at least one turn.

    A conversation has a turn when it has a prompt or a response with no parent
    prompt (the preamble turn): a forked sub-agent with no prompts still has
    content to index, search and show. This is the SQL form of "the fold is
    non-empty" (:func:`fold_turns`), for callers that ask it of every conversation.
    """
    rows = conn.execute(
        "SELECT DISTINCT conversation_id FROM events"
        " WHERE kind = 'prompt' OR (kind = 'response' AND parent_id IS NULL)"
    ).fetchall()
    return {row[0] for row in rows}
