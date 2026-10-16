"""The shared assembler: canonical records in, conversations and a ledger out.

Every adapter used to carry four jobs in one stateful ``parse()``: decoding its
format, deciding conversation identity, building the Prompt -> Response tree,
and deciding what to keep. Only the first is tool-specific. This module owns
the other three, once, for every adapter that decodes to
:mod:`siftd.adapters.records`.

The pipeline is ``read -> decode -> assemble``:

- ``read(source)`` yields raw units. A unit it cannot parse is yielded as
  :class:`~siftd.adapters.records.Malformed`, never skipped.
- ``decoder.decode(unit)`` returns the records that unit means. An empty
  return becomes :class:`~siftd.adapters.records.Unknown`, so decoding is total
  by construction: no unit can vanish between the reader and the ledger.
  ``decoder.finish()``, if defined, returns records only the end of the stream
  can produce.
- :class:`Assembler` places each record and gives it one
  :class:`~siftd.adapters.records.Disposition`.

Placement rules, which are the decisions the adapters each used to make alone:

- **Orphans.** A response (or a tool call with no response to hold it) that
  arrives before any prompt goes to ``Conversation.preamble`` and is stored
  with no parent. It is never dropped, and no prompt is invented for it.
- **Containers.** A tool call with no response since the last prompt gets an
  empty response to hold it; the ledger counts each one as ``synthesized``.
- **Merge.** A prompt or response whose ``key`` was already seen in this
  conversation merges into that event (content appended, usage snapshot
  replaced, first id and timestamp kept).
- **Pairing.** A tool result patches the earliest open call with its
  ``call_id``, wherever that call is; a result that arrives first waits for
  its call. A non-final result updates the call and leaves it open. A result
  still waiting at the end is an ``ORPHAN``. A completed call's timestamp is
  the result's; the call was made at its response's time.
- **Usage.** Snapshots (on a response) replace; increments (``UsageRec``) sum.
  An unkeyed increment or patch that arrives before any response waits for
  the next one.
- **Explicit keys are never redirected.** A tool call, increment or patch
  naming a key not yet seen waits for that response; if it never appears the
  record is an ``ORPHAN``, not attached to some other response.
- **Time.** Every record's timestamp is evidence, including records that
  create no event. An undated event takes the nearest earlier time, or
  failing that the nearest later one, and the bounds span all of them.
  Parsing never reads the wall clock, so it is a pure function of the source.
- **Identity.** ``Session`` records open conversations; a restated
  ``Session`` fills only what is still unknown. A source with no ``Session``
  uses the caller's ``default_session``, whose fields sit beneath anything
  the stream stated; with neither, assembly fails loudly. ``ended_at`` is
  inferred from event times unless a Session says the source cannot know it
  (``infer_end=False``).
- **Lineage.** A conversation has at most one parent per relation: where it
  ran (``subagent``) and what it continues (``fork``, ``resume``, ``branch``),
  so a forked subagent keeps both. Each becomes a ``Conversation.lineage`` link
  (one ``conversation_links`` row); a second of the same relation is
  ``IGNORED``. The parent stays the
  source's identity string: resolving it to a stored conversation is a read-time
  join, not something assembly can know.
- **Arrival order.** A tool call takes its pairing slot and lends its time
  when it arrives, even if its parent response appears later; waiting
  records apply in the order they arrived. Model and provider inherited
  from ``Meta`` context yield to a later chunk that states them.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from siftd.adapters.records import (
    Disposition,
    Ignored,
    LineageRec,
    Malformed,
    Meta,
    PatchRec,
    PromptRec,
    Record,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
    UsageRec,
)
from siftd.domain import Conversation, Harness, Lineage, Prompt, Response, ToolCall, Usage
from siftd.domain.models import CONTAINMENT_KINDS
from siftd.domain.source import Source
from siftd.errors import SiftdError

#: Origin of records a decoder returns from ``finish()`` rather than for a unit.
END_OF_STREAM = -1


class AssemblyError(SiftdError):
    """A decoder produced records the assembler cannot interpret (a decoder bug)."""


class Decoder(Protocol):
    """Turns one raw unit into the records it means. May keep state between units."""

    def decode(self, unit: Any) -> list[Record]: ...


@dataclass
class Ledger:
    """What became of every unit and record from one source.

    ``units`` counts what the reader yielded. ``dispositions`` counts records by
    ``(record type, disposition)``; ``reasons`` counts the named reasons behind
    IGNORED, UNKNOWN and ORPHAN. Every record has exactly one disposition, and
    every unit produced at least one record, so ``records == sum(dispositions)``
    and nothing the source held is unaccounted for.
    """

    units: int = 0
    records: int = 0
    dispositions: Counter[tuple[str, Disposition]] = field(default_factory=Counter)
    reasons: Counter[tuple[Disposition, str]] = field(default_factory=Counter)
    synthesized: int = 0
    empty_conversations: int = 0

    def record(self, rec: Record, disposition: Disposition, reason: str | None = None) -> None:
        self.dispositions[(type(rec).__name__, disposition)] += 1
        if reason is not None:
            self.reasons[(disposition, reason)] += 1

    def count(self, disposition: Disposition) -> int:
        return sum(n for (_, d), n in self.dispositions.items() if d == disposition)

    @property
    def disposed(self) -> int:
        return sum(self.dispositions.values())

    def merge(self, other: Ledger) -> None:
        self.units += other.units
        self.records += other.records
        self.dispositions.update(other.dispositions)
        self.reasons.update(other.reasons)
        self.synthesized += other.synthesized
        self.empty_conversations += other.empty_conversations


@dataclass
class Assembly:
    conversations: list[Conversation]
    ledger: Ledger


# --- Conversation state ------------------------------------------------------


@dataclass
class _Usage:
    """A response's usage, kept as its two parts so neither clobbers the other.

    The stored total is ``snapshot + increments``: merging a chunk replaces the
    snapshot, an increment adds to the increments, and the response's
    ``usage`` and usage-detail attributes are rewritten from both each time.
    A detail key a replaced snapshot no longer carries is removed.
    """

    snapshot: Usage | None = None
    snapshot_details: dict[str, int] = field(default_factory=dict)
    increments: Usage | None = None
    increment_details: dict[str, int] = field(default_factory=dict)
    written: set[str] = field(default_factory=set)

    def write(self, response: Response) -> None:
        total: Usage | None = None
        for u in (self.snapshot, self.increments):
            if u is not None:
                total = _add_usage(total, u)
        response.usage = total
        keys = self.snapshot_details.keys() | self.increment_details.keys()
        for k in self.written - keys:
            response.attributes.pop(k, None)
        for k in keys:
            response.attributes[k] = str(self.snapshot_details.get(k, 0) + self.increment_details.get(k, 0))
        self.written = set(keys)


@dataclass
class _Pending:
    """A record waiting for a response, and what it does to that response.

    ``seq`` is its arrival order: waiting records from different queues are
    applied in the order they arrived, so later-wins holds across them.
    """

    seq: int
    rec: Record
    apply: Callable[[Response], None]


class _Build:
    """One conversation under construction."""

    def __init__(self) -> None:
        # Identity and bounds, from Session records (first stated wins; a
        # restatement fills only what is still unknown).
        self.external_id: str | None = None
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.infer_end: bool | None = None
        self.workspace_path: str | None = None
        self.branch: str | None = None
        self.attributes: dict[str, str] = {}
        # Context for later responses (a later Meta replaces it).
        self.model: str | None = None
        self.provider: str | None = None

        self.prompts: list[Prompt] = []
        self.preamble: list[Response] = []
        self.prompts_by_key: dict[str, Prompt] = {}
        self.responses_by_key: dict[str, Response] = {}
        self.latest_response: Response | None = None
        # Has a response arrived since the latest prompt? Decides whether a
        # tool call needs a container.
        self.response_since_prompt = False
        self.usage: dict[int, _Usage] = {}  # id(response) -> its usage parts
        # id(response) -> fields that came from Meta context, not the message;
        # a later chunk that states one explicitly replaces it.
        self.inherited: dict[int, set[str]] = {}
        self.seq = 0

        # call_id -> calls still waiting for their final result, earliest first
        self.unresulted: dict[str, list[ToolCall]] = {}
        # call_id -> results that arrived before their call, in order
        self.early_results: dict[str, list[ToolResultRec]] = {}
        # unkeyed increments and patches waiting for the next response
        self.waiting: list[_Pending] = []
        # key -> increments and patches naming a response not yet seen
        self.waiting_for_key: dict[str, list[_Pending]] = {}
        # key -> tool calls naming a parent response not yet seen. The call
        # exists (and pairs with results) from arrival; only its placement waits.
        self.calls_for_key: dict[str, list[tuple[ToolCallRec, ToolCall]]] = {}
        # id(call) -> results applied to a call not yet placed; they count as
        # PATCH once it is placed, ORPHAN if it never is.
        self.results_on_unplaced: dict[int, list[ToolResultRec]] = {}
        self.placed: set[int] = set()
        self.lineage: list[Lineage] = []

        # Every record's timestamp in arrival order, with the event it created
        # (or None for records that only lend their time: merged chunks,
        # results, usage, metadata). Fills undated events and sets the bounds.
        self.timeline: list[tuple[Prompt | Response | ToolCall | None, str | None]] = []

    @property
    def empty(self) -> bool:
        return not self.prompts and not self.preamble

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def anchor(self, at: str | None) -> None:
        if at is not None:
            self.timeline.append((None, at))


class Assembler:
    """Places records into conversations; one instance per source."""

    def __init__(self, harness: Harness, *, default_session: Session | None = None):
        self.harness = harness
        self.default_session = default_session
        self.ledger = Ledger()
        self.conversations: list[Conversation] = []
        self._build: _Build | None = None

    # -- entry points --

    def feed(self, rec: Record) -> None:
        self.ledger.records += 1
        handler = self._HANDLERS.get(type(rec))
        if handler is None:
            raise AssemblyError(f"decoder returned a non-record: {rec!r}")
        handler(self, rec)

    def finish(self) -> Assembly:
        self._close()
        if self.ledger.disposed != self.ledger.records:
            raise AssemblyError(
                f"ledger out of balance: {self.ledger.records} records, {self.ledger.disposed} dispositions"
            )
        return Assembly(self.conversations, self.ledger)

    # -- conversation lifecycle --

    def _current(self) -> _Build:
        if self._build is None:
            self._build = _Build()
        return self._build

    def _close(self) -> None:
        build = self._build
        self._build = None
        if build is None:
            return
        for pending in build.waiting:
            self.ledger.record(pending.rec, Disposition.ORPHAN, "nothing to attach to")
        for pendings in build.waiting_for_key.values():
            for pending in pendings:
                self.ledger.record(pending.rec, Disposition.ORPHAN, "target key never appeared")
        for calls in build.calls_for_key.values():
            for rec, call in calls:
                self.ledger.record(rec, Disposition.ORPHAN, "parent key never appeared")
                for res in build.results_on_unplaced.pop(id(call), []):
                    self.ledger.record(res, Disposition.ORPHAN, "its call was never placed")
        for results in build.early_results.values():
            for res in results:
                self.ledger.record(res, Disposition.ORPHAN, "tool result with no call")
        if build.empty:
            self.ledger.empty_conversations += 1
            return

        # The caller's default sits beneath anything the stream stated.
        default = self.default_session
        external_id = build.external_id or (default.external_id if default else None)
        if external_id is None:
            raise AssemblyError("the decoder produced events but no Session, and no default_session was given")
        started_at = build.started_at or (default.started_at if default else None)
        ended_at = build.ended_at or (default.ended_at if default else None)
        infer_end = build.infer_end if build.infer_end is not None else (default.infer_end if default else True)

        times = _fill_timestamps(build.timeline)
        if not times:
            # No record is dated: the session's own bounds are the only evidence.
            fallback = started_at or ended_at
            if fallback is None:
                raise AssemblyError(f"no timestamp anywhere in conversation {external_id}")
            for event, _ in build.timeline:
                if event is not None and not isinstance(event, ToolCall):
                    event.timestamp = fallback
            times = [fallback]
        self.conversations.append(
            Conversation(
                external_id=external_id,
                harness=self.harness,
                started_at=started_at or min(times),
                ended_at=ended_at or (max(times) if infer_end else None),
                prompts=build.prompts,
                preamble=build.preamble,
                workspace_path=build.workspace_path or (default.workspace_path if default else None),
                branch=build.branch or (default.branch if default else None),
                attributes={**(default.attributes if default else {}), **build.attributes},
                lineage=tuple(build.lineage),
            )
        )

    # -- handlers --

    def _on_session(self, rec: Session) -> None:
        build = self._build
        if build is not None and build.external_id is not None and build.external_id != rec.external_id:
            self._close()
            build = None
        build = build or self._current()
        if build.external_id is None:
            # A new conversation, or content that arrived first: this names it.
            build.external_id = rec.external_id
        build.started_at = build.started_at or rec.started_at
        build.ended_at = build.ended_at or rec.ended_at
        if build.infer_end is None or not rec.infer_end:
            build.infer_end = rec.infer_end
        build.workspace_path = build.workspace_path or rec.workspace_path
        build.branch = build.branch or rec.branch
        for k, v in rec.attributes.items():
            build.attributes.setdefault(k, v)
        self.ledger.record(rec, Disposition.META)

    def _on_meta(self, rec: Meta) -> None:
        build = self._current()
        build.anchor(rec.at)
        build.workspace_path = build.workspace_path or rec.workspace_path
        build.branch = build.branch or rec.branch
        if rec.model is not None:
            build.model = rec.model
        if rec.provider is not None:
            build.provider = rec.provider
        build.attributes.update(rec.attributes)
        self.ledger.record(rec, Disposition.META)

    def _on_prompt(self, rec: PromptRec) -> None:
        build = self._current()
        existing = build.prompts_by_key.get(rec.key) if rec.key else None
        if existing is not None:
            existing.content.extend(rec.content)
            build.anchor(rec.at)
            self.ledger.record(rec, Disposition.MERGE)
            return
        prompt = Prompt(timestamp=rec.at or "", content=list(rec.content), external_id=rec.external_id)
        build.prompts.append(prompt)
        build.timeline.append((prompt, rec.at))
        if rec.key:
            build.prompts_by_key[rec.key] = prompt
        build.response_since_prompt = False
        self.ledger.record(rec, Disposition.EVENT)

    def _attach_response(self, build: _Build, response: Response, at: str | None, key: str | None) -> None:
        if build.prompts:
            build.prompts[-1].responses.append(response)
        else:
            build.preamble.append(response)
        build.timeline.append((response, at))
        build.latest_response = response
        build.response_since_prompt = True
        waiting, build.waiting = build.waiting, []
        if key is not None:
            build.responses_by_key[key] = response
            waiting += build.waiting_for_key.pop(key, [])
        for pending in sorted(waiting, key=lambda p: p.seq):
            pending.apply(response)
            self.ledger.record(pending.rec, Disposition.PATCH)
        if key is not None:
            for rec, call in build.calls_for_key.pop(key, []):
                self._place_call(rec, call, response)

    def _on_response(self, rec: ResponseRec) -> None:
        build = self._current()
        existing = build.responses_by_key.get(rec.key) if rec.key else None
        if existing is not None:
            existing.content.extend(rec.content)
            inherited = build.inherited.get(id(existing), set())
            if rec.model is not None and (existing.model is None or "model" in inherited):
                existing.model = rec.model
                inherited.discard("model")
            if rec.provider is not None and (existing.provider is None or "provider" in inherited):
                existing.provider = rec.provider
                inherited.discard("provider")
            existing.attributes.update(rec.attributes)
            if rec.usage is not None or rec.usage_details:
                parts = build.usage.setdefault(id(existing), _Usage())
                parts.snapshot, parts.snapshot_details = rec.usage, dict(rec.usage_details)
                parts.write(existing)
            build.anchor(rec.at)
            self.ledger.record(rec, Disposition.MERGE)
            return
        response = Response(
            timestamp=rec.at or "",
            content=list(rec.content),
            model=rec.model or build.model,
            provider=rec.provider or build.provider,
            external_id=rec.external_id,
            attributes=dict(rec.attributes),
        )
        inherited = {
            f
            for f, own, ctx in (("model", rec.model, build.model), ("provider", rec.provider, build.provider))
            if own is None and ctx is not None
        }
        if inherited:
            build.inherited[id(response)] = inherited
        if rec.usage is not None or rec.usage_details:
            parts = build.usage.setdefault(id(response), _Usage())
            parts.snapshot, parts.snapshot_details = rec.usage, dict(rec.usage_details)
            parts.write(response)
        # Recorded before attaching, so the records waiting on it patch an
        # EVENT that is already counted.
        self.ledger.record(rec, Disposition.EVENT)
        self._attach_response(build, response, rec.at, rec.key)

    def _on_tool_call(self, rec: ToolCallRec) -> None:
        build = self._current()
        # The call exists from arrival: it takes its place in the timeline and
        # in pairing order now, even if its parent response has not appeared.
        call = ToolCall(
            tool_name=rec.name,
            input=rec.input,
            external_id=rec.call_id,
            timestamp=rec.at,
            attributes=dict(rec.attributes),
        )
        build.timeline.append((call, rec.at))
        if rec.call_id is not None:
            # Results that arrived first, in order, up to and including the final one.
            early = build.early_results.get(rec.call_id, [])
            completed = False
            while early and not completed:
                res = early.pop(0)
                _apply_result(call, res)
                build.results_on_unplaced.setdefault(id(call), []).append(res)
                completed = res.final
            if not early:
                build.early_results.pop(rec.call_id, None)
            if not completed:
                build.unresulted.setdefault(rec.call_id, []).append(call)

        if rec.parent_key is not None:
            parent = build.responses_by_key.get(rec.parent_key)
            if parent is None:
                # Named a response not seen yet: wait for it, never guess.
                build.calls_for_key.setdefault(rec.parent_key, []).append((rec, call))
                return
        elif build.response_since_prompt and build.latest_response is not None:
            parent = build.latest_response
        else:
            parent = Response(timestamp=rec.at or "", model=build.model, provider=build.provider)
            self.ledger.synthesized += 1
            self._attach_response(build, parent, rec.at, None)
        self._place_call(rec, call, parent)

    def _place_call(self, rec: ToolCallRec, call: ToolCall, parent: Response) -> None:
        build = self._current()
        parent.tool_calls.append(call)
        build.placed.add(id(call))
        self.ledger.record(rec, Disposition.EVENT)
        for res in build.results_on_unplaced.pop(id(call), []):
            self.ledger.record(res, Disposition.PATCH)

    def _on_tool_result(self, rec: ToolResultRec) -> None:
        build = self._current()
        build.anchor(rec.at)
        calls = build.unresulted.get(rec.call_id)
        if not calls:
            build.early_results.setdefault(rec.call_id, []).append(rec)
            return
        call = calls[0]
        _apply_result(call, rec)
        if rec.final:
            calls.pop(0)
            if not calls:
                del build.unresulted[rec.call_id]
        if id(call) in build.placed:
            self.ledger.record(rec, Disposition.PATCH)
        else:
            build.results_on_unplaced.setdefault(id(call), []).append(rec)

    def _on_usage(self, rec: UsageRec) -> None:
        build = self._current()
        build.anchor(rec.at)

        def apply(response: Response) -> None:
            parts = build.usage.setdefault(id(response), _Usage())
            parts.increments = _add_usage(parts.increments, rec.usage)
            for k, v in rec.usage_details.items():
                parts.increment_details[k] = parts.increment_details.get(k, 0) + v
            parts.write(response)

        self._patch_or_wait(build, rec, rec.target_key, apply)

    def _on_patch(self, rec: PatchRec) -> None:
        build = self._current()
        build.anchor(rec.at)
        self._patch_or_wait(build, rec, rec.target_key, lambda r: r.attributes.update(rec.attributes))

    def _patch_or_wait(self, build: _Build, rec: Record, key: str | None, apply: Callable[[Response], None]) -> None:
        if key is not None:
            target = build.responses_by_key.get(key)
            if target is None:
                # An explicit target is never redirected to another response.
                build.waiting_for_key.setdefault(key, []).append(_Pending(build.next_seq(), rec, apply))
                return
        else:
            target = build.latest_response
            if target is None:
                build.waiting.append(_Pending(build.next_seq(), rec, apply))
                return
        apply(target)
        self.ledger.record(rec, Disposition.PATCH)

    def _on_lineage(self, rec: LineageRec) -> None:
        build = self._current()
        build.anchor(rec.at)
        containment = rec.kind in CONTAINMENT_KINDS
        if any((link.kind in CONTAINMENT_KINDS) == containment for link in build.lineage):
            self.ledger.record(rec, Disposition.IGNORED, "conversation already has a parent")
            return
        build.lineage.append(Lineage(kind=rec.kind, parent=rec.parent, anchor=rec.anchor))
        self.ledger.record(rec, Disposition.LINEAGE)

    def _on_ignored(self, rec: Ignored) -> None:
        self.ledger.record(rec, Disposition.IGNORED, rec.reason)

    def _on_unknown(self, rec: Unknown) -> None:
        self.ledger.record(rec, Disposition.UNKNOWN, rec.reason)

    _HANDLERS: dict[type, Callable[[Assembler, Any], None]] = {
        Session: _on_session,
        Meta: _on_meta,
        PromptRec: _on_prompt,
        ResponseRec: _on_response,
        ToolCallRec: _on_tool_call,
        ToolResultRec: _on_tool_result,
        UsageRec: _on_usage,
        PatchRec: _on_patch,
        LineageRec: _on_lineage,
        Ignored: _on_ignored,
        Unknown: _on_unknown,
    }


def _apply_result(call: ToolCall, rec: ToolResultRec) -> None:
    call.result = rec.result
    call.status = rec.status
    call.attributes.update(rec.attributes)
    # A completed call is stamped with its completion time; when it was made
    # is its response's time. Both instants survive, so tool duration does too.
    if rec.at is not None:
        call.timestamp = rec.at


def _add_usage(a: Usage | None, b: Usage) -> Usage:
    if a is None:
        return Usage(input_tokens=b.input_tokens, output_tokens=b.output_tokens)

    def add(x: int | None, y: int | None) -> int | None:
        return None if x is None and y is None else (x or 0) + (y or 0)

    return Usage(input_tokens=add(a.input_tokens, b.input_tokens), output_tokens=add(a.output_tokens, b.output_tokens))


def _fill_timestamps(timeline: list[tuple[Prompt | Response | ToolCall | None, str | None]]) -> list[str]:
    """Give every undated event a timestamp from its neighbours; return every given time.

    The timeline holds each record's own timestamp in arrival order, including
    records that created no event (merged chunks, results, usage, metadata):
    they are evidence of when things happened. An undated prompt or response
    takes the nearest earlier given time, or failing that the nearest later
    one. An undated tool call keeps ``None``: storage falls back to its
    response's time.
    """
    given = [at for _, at in timeline if at is not None]
    if not given:
        return []
    last: str | None = None
    unfilled: list[Prompt | Response] = []
    for event, at in timeline:
        if at is not None:
            last = at
            for e in unfilled:
                e.timestamp = last
            unfilled = []
        elif event is not None and not isinstance(event, ToolCall):
            if last is not None:
                event.timestamp = last
            else:
                unfilled.append(event)
    return given


# --- Driving a decoder over a source -------------------------------------------


def read_jsonl(source: Source) -> Iterator[dict | Malformed]:
    """Yield each JSONL line of ``source`` as a dict, or :class:`Malformed`.

    Blank lines are not units. A line that is not JSON, or is JSON but not an
    object, is yielded as ``Malformed`` so the ledger counts it, where
    ``_jsonl.load_jsonl`` skips it without a trace.
    """
    # Bytes, decoded per line: a write truncated mid-character spoils only
    # its own line, not the rest of the file.
    with source.as_path.open("rb") as f:
        for line_no, raw in enumerate(f, start=1):
            try:
                stripped = raw.decode("utf-8").strip()
            except UnicodeDecodeError as e:
                yield Malformed(line=line_no, error=str(e))
                continue
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as e:
                yield Malformed(line=line_no, error=str(e))
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                yield Malformed(line=line_no, error=f"not an object: {type(obj).__name__}")


def decode(units: Iterable[Any], decoder: Decoder) -> Iterator[tuple[int, Record]]:
    """Yield ``(origin, record)`` for every unit; total by construction.

    A :class:`Malformed` unit becomes ``Unknown`` without reaching the decoder,
    and a unit that decodes to nothing becomes ``Unknown``. Records from the
    decoder's ``finish()`` carry origin :data:`END_OF_STREAM`.
    """
    for origin, unit in enumerate(units):
        if isinstance(unit, Malformed):
            yield origin, Unknown(reason="malformed")
            continue
        records = decoder.decode(unit)
        if not records:
            yield origin, Unknown()
            continue
        for rec in records:
            yield origin, rec
    finish = getattr(decoder, "finish", None)
    if finish is not None:
        for rec in finish():
            yield END_OF_STREAM, rec


def assemble(
    source: Source,
    *,
    read: Callable[[Source], Iterable[Any]],
    decoder: Callable[[Source], Decoder],
    harness: Harness,
    default_session: Session | None = None,
) -> Assembly:
    """Read, decode and assemble one source."""
    assembler = Assembler(harness, default_session=default_session)
    units = 0
    last_origin = END_OF_STREAM
    for origin, rec in decode(read(source), decoder(source)):
        if origin != END_OF_STREAM and origin != last_origin:
            units += 1
            last_origin = origin
        assembler.feed(rec)
    assembler.ledger.units = units
    return assembler.finish()
