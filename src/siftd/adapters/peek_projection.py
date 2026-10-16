"""Peek built from the assembler: one classification for peek and ingest.

The record folds in :mod:`siftd.adapters.sdk` (``peek_scan_from_records`` and
``peek_exchanges_from_records``) classify records a second time, through a
``normalize_record`` that is not the decoder ingest uses. Where the two
disagree, peek shows a session ingest would not store: a pi tool call is
invisible, and an assistant-only session is hidden from the scan while the
exchanges list it. This module builds peek from the same
:class:`~siftd.domain.Conversation` that ingest stores, so there is one
classification.

It lives here rather than in ``sdk.py``: the folds it supersedes serve version 1
only and leave with it (``api/compat.py``, ``V1_INTERFACE_REMOVAL``), while this
module serves version 2. The text, placeholder and hint helpers it reuses stay in
``sdk.py``.

Peek reads one file, so one file must yield one conversation. A source that
yields several raises :class:`~siftd.adapters.assembly.AssemblyError` rather
than showing an arbitrary one, unless the adapter names which one is live
(``last_of_many``, aider's history file).

What the projection shows:

- A preamble (responses with no prompt before the first one) is one exchange
  with ``prompt_text`` None, placed first, marked ``preamble`` and, when the
  conversation continues another, ``continued_from`` (the stored read's header
  words, with the parent spelled as the source gave it). It is counted in
  ``exchange_count``, so the scan and the exchanges always agree.
- A response's tool calls come from ``Response.tool_calls``. A ``tool_use``
  content block that matches a call by id marks that call's position in the
  text; calls without a matching block follow the content.
- Tool names are canonicalized through the adapter's ``TOOL_ALIASES`` in the
  tool counts and the narrative's tool calls, as the record folds did. The
  response text's ``[tool: name]`` placeholders keep the raw name, also as
  the folds did. The assembler stores raw names, so aliases are an explicit
  input here, not something the conversation carries.
- A subagent whose own identity is nested under its parent's (Claude Code:
  ``<parent>::agent::<agent>``) is shown as ``<parent>:<agent>``, the spelling
  peek has always used. A subagent whose identity is its own (Codex: its thread
  id) is shown by that id, which is also what its file name carries. The
  containment comes from the lineage record and is carried as
  ``parent_session_id`` either way.
- Token counts are the response's usage, which the assembler keeps as one
  total per message, so streamed chunks of one message are not summed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from siftd.adapters.assembly import Assembly, AssemblyError, Decoder, assemble
from siftd.adapters.records import Session
from siftd.adapters.sdk import (
    _is_tool_placeholder_only,
    canonicalize_tool_name,
    extract_text_with_placeholders,
    peek_jsonl_tail,
    peek_tool_hint,
)
from siftd.domain import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall
from siftd.domain.models import continued_link
from siftd.domain.peek import PeekExchange, PeekNarrativeBlock, PeekScanResult, PeekToolCall
from siftd.domain.source import Source


def peek_scan_from_assembly(assembly: Assembly) -> PeekScanResult | None:
    """Scan metadata for the one conversation in ``assembly``, or None if it has none."""
    conversation = _only_conversation(assembly)
    if conversation is None:
        return None

    model: str | None = None
    for response in _responses(conversation):
        model = response.model or model

    # A subagent's parent is the session it ran in, even when it also forked from another.
    link = conversation.parent("subagent") or next(iter(conversation.lineage), None)
    return PeekScanResult(
        session_id=_session_id(conversation),
        workspace_path=conversation.workspace_path,
        model=model,
        exchange_count=conversation.turn_count,
        started_at=conversation.started_at,
        last_activity_at=conversation.ended_at,
        parent_session_id=(_session_part(conversation.harness, link.parent) if link is not None else None),
        attributes=dict(conversation.attributes),
    )


def peek_exchanges_from_assembly(
    assembly: Assembly,
    last_n: int,
    *,
    include_thinking: bool = False,
    tool_aliases: Mapping[str, str] | None = None,
) -> list[PeekExchange]:
    """The last ``last_n`` exchanges of the one conversation in ``assembly``, oldest first."""
    conversation = _only_conversation(assembly)
    if conversation is None:
        return []
    if last_n < 1:
        last_n = 1

    exchanges: list[PeekExchange] = []
    if conversation.preamble:
        link = continued_link(conversation.lineage)
        preamble = PeekExchange(
            timestamp=conversation.preamble[0].timestamp or None,
            preamble=True,
            continued_from=f"{link.parent} ({link.kind})" if link is not None else None,
        )
        _fill_exchange(preamble, conversation.preamble, include_thinking=include_thinking, tool_aliases=tool_aliases)
        exchanges.append(preamble)
    for prompt in conversation.prompts:
        exchange = PeekExchange(
            timestamp=prompt.timestamp or None,
            prompt_text=_prompt_text(prompt),
            prompt_external_id=prompt.external_id,
        )
        _fill_exchange(exchange, prompt.responses, include_thinking=include_thinking, tool_aliases=tool_aliases)
        exchanges.append(exchange)
    return exchanges[-last_n:]


def make_peek_hooks_from_decoder(
    read: Callable[[Source], Iterable[Any]],
    decoder: Callable[[Source], Decoder],
    harness: Harness,
    default_session_for: Callable[[Path], Session | None],
    tool_aliases: Mapping[str, str] | None = None,
    last_of_many: bool = False,
) -> tuple[
    Callable[[Path], PeekScanResult | None],
    Callable[..., list[PeekExchange]],
    Callable[[Path, int], Iterator[dict | str]],
]:
    """Peek hooks for an adapter that decodes to canonical records.

    The arguments are the ones :func:`siftd.adapters.assembly.assemble` takes,
    plus ``default_session_for``, which names the session of a file whose
    decoder states none (usually ``Session(external_id=f"{NAME}::{path.stem}")``),
    and ``tool_aliases``, the adapter's ``TOOL_ALIASES``. Passing the adapter's
    aliases is what keeps peek's tool names the ones the record folds showed.
    ``last_of_many`` is for a container source (aider's history file, which
    every session is appended to). Peek shows its last conversation, the live
    one, instead of refusing a file that holds several.

    Returns ``(peek_scan, peek_exchanges, peek_tail)`` with the signatures
    :func:`siftd.adapters.sdk.make_peek_hooks` returns. An adapter does not call
    this itself: :mod:`siftd.adapters.contract` derives its hooks from its
    ``read`` and ``decoder`` declarations.
    """

    def assembled(path: Path) -> Assembly:
        assembly = assemble(
            Source(kind="file", location=path),
            read=read,
            decoder=decoder,
            harness=harness,
            default_session=default_session_for(path),
        )
        if last_of_many and assembly.conversations:
            return Assembly(conversations=assembly.conversations[-1:], ledger=assembly.ledger)
        return assembly

    def peek_scan(path: Path) -> PeekScanResult | None:
        return peek_scan_from_assembly(assembled(path))

    def peek_exchanges(
        path: Path,
        last_n: int = 5,
        *,
        include_thinking: bool = False,
    ) -> list[PeekExchange]:
        return peek_exchanges_from_assembly(
            assembled(path), last_n, include_thinking=include_thinking, tool_aliases=tool_aliases
        )

    def peek_tail(path: Path, lines: int = 20) -> Iterator[dict | str]:
        yield from peek_jsonl_tail(path, lines, parse_json=True)

    return peek_scan, peek_exchanges, peek_tail


# --- helpers -----------------------------------------------------------------


def _only_conversation(assembly: Assembly) -> Conversation | None:
    conversations = assembly.conversations
    if not conversations:
        return None
    if len(conversations) > 1:
        raise AssemblyError(f"peek reads one conversation per file; this source holds {len(conversations)}")
    return conversations[0]


def _session_part(harness: Harness, external_id: str) -> str:
    """The session part of an external id: adapters spell it ``<harness>::<session>``."""
    return external_id.removeprefix(f"{harness.name}::")


def _session_id(conversation: Conversation) -> str:
    """The session identity peek shows for a conversation.

    A subagent whose external id nests under its parent's
    (``<harness>::<parent>::agent::<agent>``, Claude Code) is shown as
    ``<parent>:<agent>``, the agent being the id's last segment. Any other
    subagent is shown by its own id, because that is the id its stored identity
    and its file name carry: a Codex subagent's thread id is not built from its
    parent's. Only the lineage record marks a subagent, so the parent named here
    and in ``parent_session_id`` come from the same fact.
    """
    own = _session_part(conversation.harness, conversation.external_id)
    subagent = conversation.parent("subagent")
    if subagent is not None:
        parent = _session_part(conversation.harness, subagent.parent)
        if own.startswith(f"{parent}::"):
            return f"{parent}:{own.rsplit('::', 1)[-1]}"
    return own


def _responses(conversation: Conversation) -> Iterator[Response]:
    yield from conversation.preamble
    for prompt in conversation.prompts:
        yield from prompt.responses


def _block_dict(block: ContentBlock) -> dict:
    return {**block.content, "type": block.block_type}


def _prompt_text(prompt: Prompt) -> str | None:
    return extract_text_with_placeholders([_block_dict(b) for b in prompt.content])


def _ordered_parts(response: Response) -> list[dict | ToolCall]:
    """A response's content and tool calls, in order.

    A ``tool_use`` content block that matches one of the response's calls by id
    stands for that call. Calls with no matching block follow the content.
    """
    by_id = {tc.external_id: tc for tc in response.tool_calls if tc.external_id is not None}
    placed: set[int] = set()
    parts: list[dict | ToolCall] = []
    for block in response.content:
        content = _block_dict(block)
        if block.block_type == "tool_use":
            call = by_id.get(content.get("id"))
            if call is not None and id(call) not in placed:
                placed.add(id(call))
                parts.append(call)
                continue
        parts.append(content)
    parts.extend(tc for tc in response.tool_calls if id(tc) not in placed)
    return parts


def _as_block(part: dict | ToolCall) -> dict:
    """A tool call as the block the placeholder helpers render: ``[tool: name]``."""
    if isinstance(part, ToolCall):
        return {"type": "tool_use", "name": part.tool_name, "input": part.input, "id": part.external_id}
    return part


def _fill_exchange(
    exchange: PeekExchange,
    responses: Iterable[Response],
    *,
    include_thinking: bool,
    tool_aliases: Mapping[str, str] | None,
) -> None:
    """Fold the responses of one exchange into it, as the record fold did."""
    aliases = dict(tool_aliases or {})
    counter: Counter[str] = Counter()
    for response in responses:
        if response.external_id is not None:
            exchange.response_external_ids.append(response.external_id)
        if response.usage is not None:
            exchange.input_tokens += response.usage.input_tokens or 0
            exchange.output_tokens += response.usage.output_tokens or 0

        parts = _ordered_parts(response)
        # First response with real text wins; a response of only tool placeholders does not count.
        text = extract_text_with_placeholders([_as_block(p) for p in parts], include_thinking=include_thinking)
        if text and not exchange.response_text and not _is_tool_placeholder_only(text):
            exchange.response_text = text

        # Consecutive calls form one group, emitted where they sit: a text or
        # thinking block after them closes the group, so before -> call -> after
        # renders in that order.
        calls: list[PeekToolCall] = []

        def flush() -> None:
            if calls:
                exchange.narrative.append(PeekNarrativeBlock(block_type="tool_calls", tool_calls=list(calls)))
                calls.clear()

        for part in parts:
            if isinstance(part, ToolCall):
                tool_name = canonicalize_tool_name(part.tool_name, aliases)
                counter[tool_name] += 1
                calls.append(
                    PeekToolCall(
                        tool_name=tool_name,
                        input=peek_tool_hint(part.input),
                        external_id=part.external_id,
                    )
                )
                if part.external_id:
                    exchange.tool_use_ids.append(str(part.external_id))
                continue
            block_type = part.get("type")
            if block_type == "text":
                text_val = part.get("text", "")
                if text_val:
                    flush()
                    exchange.narrative.append(PeekNarrativeBlock(block_type="text", content=text_val))
            elif block_type == "thinking" and include_thinking:
                text_val = part.get("thinking") or part.get("text") or ""
                if text_val:
                    flush()
                    exchange.narrative.append(PeekNarrativeBlock(block_type="thinking", content=text_val))
        flush()

    if counter:
        exchange.tool_calls = list(counter.most_common())
