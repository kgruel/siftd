"""Codex CLI adapter for siftd.

Pure parser: reads JSONL session files and yields Conversation domain objects.
No storage coupling.
"""

from collections.abc import Iterable, Iterator
from pathlib import Path

from siftd.adapters._jsonl import load_jsonl, now_iso
from siftd.adapters.sdk import (
    NormalizedRecord,
    build_harness,
    discover_files,
    flush_pending_calls,
    iter_jsonl,
    make_peek_hooks,
)
from siftd.domain import (
    ContentBlock,
    Conversation,
    Prompt,
    Response,
    Source,
    ToolCall,
    Usage,
)
from siftd.safecall import parse_json_args

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 1
SUPPORT_TIER = "core"
NAME = "codex_cli"
DEFAULT_LOCATIONS = ["~/.codex/sessions"]
DEDUP_STRATEGY = "file"  # one conversation per file

# Harness metadata
HARNESS_SOURCE = "openai"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Codex CLI"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {
    "shell_command": "shell.execute",
    "exec_command": "shell.execute",
    "exec": "shell.execute",
    "wait": "shell.wait",
    "apply_patch": "file.edit",
    "update_plan": "ui.todo",
    "view_image": "file.read",
    "write_stdin": "shell.stdin",
}


def _drop_reemitted_token_counts(records: Iterable[dict]) -> Iterator[dict]:
    """Yield records with token_count re-emissions removed.

    Codex writes a token_count per API call, but it also re-logs the previous
    token_count (same cumulative total, later timestamp), most often right
    after a turn_context (14,624 of 18,435 repeats in the local corpus). The
    repeat usually also carries the same last_token_usage (18,148 of 18,435),
    but the cumulative total is the signal: a repeat means no new call. The
    cumulative total moves on every call, and its per-call deltas equal
    last_token_usage in every such event. Dropping repeats makes the per-call
    sum equal the final total_token_usage in 1,041 of 1,043 corpus files; the
    other two contain a total reset.

    Only an event that carries last_token_usage advances the watermark. A
    total-only event is not a call, so it must not make the next real call look
    like a repeat (a reproduced sequence lost a 100-token call that way). The
    corpus has no event with a total and no last_token_usage, so no corpus sum
    changes.

    Parse and peek both read through this one function, so they agree on which
    token_count events carry usage.
    """
    last_total = None
    for record in records:
        if isinstance(record, dict) and record.get("type") == "event_msg":
            payload = record.get("payload") or {}
            info = payload.get("info") if payload.get("type") == "token_count" else None
            if info and info.get("last_token_usage"):
                total = info.get("total_token_usage")
                if total is not None:
                    if total == last_total:
                        continue
                    last_total = total
        yield record


def _drop_prompt_less_assistant(records: Iterable[dict]) -> Iterator[dict]:
    """Yield records without assistant messages that precede the first user message.

    parse() never stores such a message (there is no prompt to hold it), so it
    is not a usage target there. Peek would otherwise show it as an exchange and
    give it the usage that parse attaches to the first stored response.
    """
    seen_user = False
    for record in records:
        if isinstance(record, dict) and record.get("type") == "response_item":
            payload = record.get("payload") or {}
            if payload.get("type") == "message":
                role = payload.get("role")
                if role == "user":
                    seen_user = True
                elif role == "assistant" and not seen_user:
                    continue
        yield record


def _iter_peek_records(path: Path) -> Iterator[dict]:
    """Record iterator for peek: the same usage-selection rules parse() applies."""
    return _drop_prompt_less_assistant(_drop_reemitted_token_counts(iter_jsonl(path)))


def _plus(total: int | None, amount: int | None) -> int | None:
    if amount is None:
        return total
    return amount if total is None else total + amount


def _add_attribute(response: Response, key: str, amount: int) -> None:
    prior = int(response.attributes.get(key, "0"))
    response.attributes[key] = str(prior + amount)


def _add_usage(response: Response, last_usage: dict) -> None:
    """Accumulate one API call's usage onto a response.

    Every field sums. A response can span several calls (one assistant turn
    with N tool calls is N token_counts), so last-wins for usage and
    first-wins for attributes each kept one call and dropped the rest.
    """
    input_tokens = last_usage.get("input_tokens")
    output_tokens = last_usage.get("output_tokens")
    if input_tokens is not None or output_tokens is not None:
        prior_in = response.usage.input_tokens if response.usage else None
        prior_out = response.usage.output_tokens if response.usage else None
        response.usage = Usage(
            input_tokens=_plus(prior_in, input_tokens),
            output_tokens=_plus(prior_out, output_tokens),
        )
    cached_input = last_usage.get("cached_input_tokens")
    if cached_input is not None:
        _add_attribute(response, "cached_input_tokens", cached_input)
        _add_attribute(response, "cache_read_input_tokens", cached_input)
    reasoning_output = last_usage.get("reasoning_output_tokens")
    if reasoning_output is not None:
        _add_attribute(response, "reasoning_output_tokens", reasoning_output)


class _UsageLedger:
    """Routes each API call's usage to exactly one stored Response.

    The rule: a token_count's last_token_usage goes to the most recent stored
    response in the conversation. Usage that arrives before any stored response
    is held and drained into the next response attached.

    Only stored responses are targets. An assistant message with no user before
    it is never appended to a prompt, so it is never attached; usage after it
    waits for the next stored response instead of landing on an object that is
    discarded.
    """

    def __init__(self) -> None:
        self._latest: Response | None = None
        self._held: list[dict] = []

    @property
    def latest(self) -> Response | None:
        """The most recent stored response, or None before the first one."""
        return self._latest

    @property
    def held_calls(self) -> int:
        """Usage events still waiting for a stored response."""
        return len(self._held)

    def attach(self, response: Response) -> None:
        """Mark a stored response as the target and drain held usage into it."""
        self._latest = response
        for last_usage in self._held:
            _add_usage(response, last_usage)
        self._held.clear()

    def record(self, last_usage: dict) -> None:
        if self._latest is None:
            self._held.append(last_usage)
        else:
            _add_usage(self._latest, last_usage)


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Codex CLI session files."""
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/*.jsonl"])


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.suffix != ".jsonl":
        return False
    # Check if file is under Codex CLI's sessions directory
    # Accept paths like ~/.codex/sessions/... or any path with "sessions" component
    path_str = str(path)
    # Check actual location
    for loc in DEFAULT_LOCATIONS:
        loc_expanded = str(Path(loc).expanduser())
        if loc_expanded in path_str:
            return True
    # Also accept if "sessions" is a path component (for tests/mock paths)
    if "sessions" in path.parts:
        return True
    return False


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Codex CLI JSONL file and yield Conversation objects."""
    path = Path(source.location)
    records = load_jsonl(path)
    if not records:
        return

    # Extract metadata from session_meta and turn_context
    session_id = None
    session_cwd = None
    model = None
    started_at = None
    ended_at = None

    for record in records:
        record_type = record.get("type")
        ts = record.get("timestamp")

        if record_type == "session_meta":
            payload = record.get("payload", {})
            session_id = payload.get("id")
            session_cwd = payload.get("cwd")
            if not started_at and ts:
                started_at = ts

        elif record_type == "turn_context":
            payload = record.get("payload", {})
            model = model or payload.get("model")

        # Track time bounds from all records
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

    # Build harness
    harness = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)

    # Build external_id
    external_id = f"{NAME}::{session_id or path.stem}"

    # Create conversation
    conversation = Conversation(
        external_id=external_id,
        harness=harness,
        started_at=started_at or now_iso(),
        ended_at=ended_at,
        workspace_path=session_cwd,
    )

    # Process records into prompts/responses
    # pending_calls tracks function_call/custom_tool_call waiting for output
    # key: call_id, value: (response, tool_name, input_data)
    pending_calls: dict[str, tuple[Response, str, dict | str]] = {}
    current_prompt: Prompt | None = None
    usage = _UsageLedger()

    for record in _drop_reemitted_token_counts(records):
        record_type = record.get("type")
        if record_type == "event_msg":
            payload = record.get("payload") or {}
            if payload.get("type") == "token_count":
                last_usage = (payload.get("info") or {}).get("last_token_usage")
                if last_usage:
                    usage.record(last_usage)
            elif payload.get("type") == "task_complete" and usage.latest is not None:
                for key in ("duration_ms", "time_to_first_token_ms"):
                    value = payload.get(key)
                    if value is not None:
                        usage.latest.attributes.setdefault(key, str(value))
            continue

        if record_type != "response_item":
            continue

        payload = record.get("payload", {})
        item_type = payload.get("type")
        timestamp = record.get("timestamp", now_iso())

        if item_type == "message":
            role = payload.get("role")
            content_blocks = payload.get("content", [])

            if role == "user":
                current_prompt = Prompt(timestamp=timestamp)
                for block in content_blocks:
                    current_prompt.content.append(_parse_block(block))
                conversation.prompts.append(current_prompt)

            elif role == "assistant":
                response = Response(
                    timestamp=timestamp,
                    model=model,
                )
                for block in content_blocks:
                    response.content.append(_parse_block(block))
                # Without a user prompt the response is not stored, so it
                # must not become the usage target (see _UsageLedger).
                if current_prompt is not None:
                    current_prompt.responses.append(response)
                    usage.attach(response)

        elif item_type == "reasoning":
            # Encrypted reasoning carries no renderable content; only the
            # summary (when present) is worth keeping, as thinking blocks.
            for text in _reasoning_summary_texts(payload):
                response, current_prompt = _get_or_create_response(
                    current_prompt, conversation, timestamp, model
                )
                response.content.append(
                    ContentBlock(block_type="thinking", content={"thinking": text})
                )
                usage.attach(response)

        elif item_type == "function_call":
            tool_name = payload.get("name", "unknown")
            arguments = payload.get("arguments", "{}")
            call_id = payload.get("call_id")

            # Parse arguments (JSON string)
            input_data = parse_json_args(arguments)

            # Create a response for the tool call if we don't have one yet
            response, current_prompt = _get_or_create_response(
                current_prompt, conversation, timestamp, model
            )

            # Add tool_use content block
            response.content.append(ContentBlock(
                block_type="tool_use",
                content={"id": call_id, "name": tool_name, "input": input_data},
            ))
            usage.attach(response)

            if call_id:
                pending_calls[call_id] = (response, tool_name, input_data)

        elif item_type == "function_call_output":
            call_id = payload.get("call_id")
            output = payload.get("output", "")

            if call_id and call_id in pending_calls:
                response, tool_name, input_data = pending_calls.pop(call_id)
                tool_call = ToolCall(
                    tool_name=tool_name,
                    input=input_data if isinstance(input_data, dict) else {"raw": input_data},
                    result={"output": output},
                    status="success",
                    external_id=call_id,
                    timestamp=timestamp,
                )
                response.tool_calls.append(tool_call)

        elif item_type == "custom_tool_call":
            tool_name = payload.get("name", "unknown")
            input_data = payload.get("input", "")
            call_id = payload.get("call_id")

            response, current_prompt = _get_or_create_response(
                current_prompt, conversation, timestamp, model
            )

            response.content.append(ContentBlock(
                block_type="tool_use",
                content={"id": call_id, "name": tool_name, "input": input_data},
            ))
            usage.attach(response)

            if call_id:
                pending_calls[call_id] = (response, tool_name, input_data)

        elif item_type == "custom_tool_call_output":
            call_id = payload.get("call_id")
            output = payload.get("output", "")

            if call_id and call_id in pending_calls:
                response, tool_name, input_data = pending_calls.pop(call_id)
                tool_call = ToolCall(
                    tool_name=tool_name,
                    input=input_data if isinstance(input_data, dict) else {"raw": input_data},
                    result={"output": output},
                    status="success",
                    external_id=call_id,
                    timestamp=timestamp,
                )
                response.tool_calls.append(tool_call)

    # Usage that never found a stored response (token_count with no message or
    # call anywhere in the file) gets a synthetic response, as orphan tool calls
    # do, so its tokens are counted rather than dropped.
    if usage.held_calls:
        response, current_prompt = _get_or_create_response(
            current_prompt,
            conversation,
            conversation.ended_at or conversation.started_at,
            model,
        )
        usage.attach(response)

    # Handle pending tool calls that never got output
    flush_pending_calls(pending_calls)

    yield conversation


def _parse_block(block) -> ContentBlock:
    """Parse content block into a ContentBlock domain object."""
    if isinstance(block, str):
        return ContentBlock(block_type="text", content={"text": block})
    block_type = block.get("type", "unknown")
    # Normalize Codex block types to common types
    if block_type == "input_text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    if block_type == "output_text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    return ContentBlock(block_type=block_type, content=block)



def _reasoning_summary_texts(payload: dict) -> list[str]:
    """Extract non-empty summary texts from a reasoning response_item.

    Codex reasoning items carry encrypted_content (opaque) plus a summary
    list of {"type": "summary_text", "text": ...}. Summaries are empty in
    current logs; this makes future summaries a data-only change instead
    of a silent drop.
    """
    texts = []
    for entry in payload.get("summary") or []:
        if isinstance(entry, dict):
            text = entry.get("text", "")
        elif isinstance(entry, str):
            text = entry
        else:
            continue
        if text:
            texts.append(text)
    return texts


def _get_or_create_response(
    current_prompt: Prompt | None,
    conversation: Conversation,
    timestamp: str,
    model: str | None,
) -> tuple[Response, Prompt]:
    """Get the latest response on the current prompt, or create one.

    If current_prompt is None (tool calls before any user message),
    creates a synthetic prompt so the response is attached to the
    conversation rather than silently dropped.
    """
    if current_prompt is None:
        current_prompt = Prompt(timestamp=timestamp)
        conversation.prompts.append(current_prompt)
    if current_prompt.responses:
        return current_prompt.responses[-1], current_prompt
    response = Response(timestamp=timestamp, model=model)
    current_prompt.responses.append(response)
    return response, current_prompt


# =============================================================================
# Record normalization — general pattern for SDK integration
# =============================================================================


def normalize_record(raw: dict) -> NormalizedRecord | None:
    """Map a Codex CLI native record to NormalizedRecord.

    Codex CLI record types:
        "session_meta"  → metadata (id, cwd)
        "turn_context"  → metadata (model)
        "response_item" with payload.type "message" → user or assistant
        "response_item" with payload.type "function_call"/"custom_tool_call" → tool_use
        "response_item" with payload.type "reasoning" → assistant (thinking, summary only)
        "event_msg" with payload.type "token_count" → usage
        "event_msg" with payload.type "task_complete" → metadata (latency attrs in extra)
    """
    record_type = raw.get("type")
    ts = raw.get("timestamp")

    if record_type == "session_meta":
        payload = raw.get("payload", {})
        return NormalizedRecord(
            kind="metadata",
            timestamp=ts,
            session_id=payload.get("id"),
            workspace_path=payload.get("cwd"),
        )

    if record_type == "turn_context":
        payload = raw.get("payload", {})
        return NormalizedRecord(
            kind="metadata",
            timestamp=ts,
            model=payload.get("model"),
        )

    if record_type == "event_msg":
        payload = raw.get("payload") or {}
        if payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last_usage = info.get("last_token_usage") or {}
            return NormalizedRecord(
                kind="usage",
                timestamp=ts,
                input_tokens=last_usage.get("input_tokens", 0) or 0,
                output_tokens=last_usage.get("output_tokens", 0) or 0,
            )
        if payload.get("type") == "task_complete":
            extra = {
                key: str(payload[key])
                for key in ("duration_ms", "time_to_first_token_ms")
                if payload.get(key) is not None
            }
            return NormalizedRecord(kind="metadata", timestamp=ts, extra=extra)
        return None

    if record_type == "response_item":
        payload = raw.get("payload", {})
        item_type = payload.get("type")

        if item_type == "message":
            role = payload.get("role")
            content_blocks = payload.get("content", [])
            # Normalize Codex block types to standard
            normalized_blocks = []
            for block in content_blocks:
                if isinstance(block, dict):
                    bt = block.get("type", "")
                    if bt in ("input_text", "output_text"):
                        normalized_blocks.append(
                            {"type": "text", "text": block.get("text", "")}
                        )
                    else:
                        normalized_blocks.append(block)
                elif isinstance(block, str):
                    normalized_blocks.append({"type": "text", "text": block})

            if role == "user":
                return NormalizedRecord(
                    kind="user",
                    timestamp=ts,
                    content_blocks=normalized_blocks,
                )
            if role == "assistant":
                return NormalizedRecord(
                    kind="assistant",
                    timestamp=ts,
                    content_blocks=normalized_blocks,
                )

        elif item_type == "reasoning":
            # Only summaries are renderable (encrypted_content is opaque);
            # empty in current logs, captured so future summaries surface.
            texts = _reasoning_summary_texts(payload)
            if not texts:
                return None
            return NormalizedRecord(
                kind="assistant",
                timestamp=ts,
                content_blocks=[{"type": "thinking", "thinking": t} for t in texts],
            )

        elif item_type in ("function_call", "custom_tool_call"):
            return NormalizedRecord(
                kind="tool_use",
                timestamp=ts,
                tool_name=payload.get("name", "unknown"),
            )

    return None


# Peek hooks — derived from normalizer
peek_scan, peek_exchanges, peek_tail = make_peek_hooks(
    normalize_record,
    tool_aliases=TOOL_ALIASES,
    record_iterator=_iter_peek_records,
)
