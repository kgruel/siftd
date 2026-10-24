"""Pi Coding Agent adapter for siftd.

Pure decoder: reads JSONL session files and yields Conversation domain objects.
No storage coupling. The adapter only decodes: :class:`PiDecoder` turns each
JSONL line into canonical records, and the shared assembler
(:mod:`siftd.adapters.assembly`) decides where they attach.
"""

import sys
from collections.abc import Iterable
from pathlib import Path

from siftd.adapters.assembly import read_jsonl
from siftd.adapters.contract import DecodingAdapter
from siftd.adapters.records import (
    Ignored,
    Meta,
    PromptRec,
    Record,
    ResponseRec,
    Session,
    ToolCallRec,
    ToolResultRec,
    Unknown,
    UsageRec,
)
from siftd.adapters.sdk import discover_files
from siftd.content.filters import filter_binary_block
from siftd.domain import ContentBlock, Conversation, Source, Usage
from siftd.safecall import parse_json_args

# Adapter self-description
ADAPTER_INTERFACE_VERSION = 2
SUPPORT_TIER = "contrib"
NAME = "pi_agent"
DEFAULT_LOCATIONS = ["~/.pi/agent/sessions"]
DEDUP_STRATEGY = "file"  # one conversation per file

# Harness metadata
HARNESS_SOURCE = "multi"
HARNESS_LOG_FORMAT = "jsonl"
HARNESS_DISPLAY_NAME = "Pi Coding Agent"

# Raw tool name → canonical tool name
TOOL_ALIASES: dict[str, str] = {}

# pi's provider names, spelled as siftd's pricing and harness sources spell
# them: the codex route serves OpenAI's models and the gemini CLI route serves
# Google's, as codex_cli and antigravity_cli store them. Any other provider
# (github-copilot, ollama, omlx) keeps its own name, under which no price is
# filed; its responses are priced at the model maker's rate when the model has
# one (storage.sql_helpers.price_provider_id_sql) and stay unpriced otherwise.
_PROVIDERS = {"openai-codex": "openai", "google-gemini-cli": "google"}

# Record types with no conversation content, skipped by name. The decoder's
# docstring says why, and tests/adapters/test_pi_agent_decoding.py keeps that
# table and these sets equal.
_IGNORED_TYPES = frozenset({"thinking_level_change", "compaction", "context_edit"})
# Message roles with no conversation content, skipped by name.
_IGNORED_ROLES = frozenset({"system", "bashExecution"})


def discover(locations=None) -> Iterable[Source]:
    """Yield Source objects for all Pi Agent session files."""
    yield from discover_files(locations, DEFAULT_LOCATIONS, ["**/*.jsonl"])


def can_handle(source: Source) -> bool:
    """Return True if this adapter can parse the given source."""
    if source.kind != "file":
        return False
    path = Path(source.location)
    if path.suffix != ".jsonl":
        return False
    path_str = str(path)
    for loc in DEFAULT_LOCATIONS:
        loc_expanded = str(Path(loc).expanduser())
        if loc_expanded in path_str:
            return True
    # Accept paths with .pi/agent/sessions component (for tests/mock paths)
    if ".pi" in path.parts:
        parent_parts = path.parts
        for i, part in enumerate(parent_parts):
            if part == ".pi" and i + 2 < len(parent_parts):
                if parent_parts[i + 1] == "agent" and parent_parts[i + 2] == "sessions":
                    return True
    return False


class PiDecoder:
    """Turns each Pi Agent JSONL line into canonical records.

    Every record `type` in the corpus is classified here; a type this table
    does not name is ``Unknown``. A decoder holds only decoding state: the
    session id the file's last header states.

    | Record | Becomes | Why |
    |---|---|---|
    | `session` | Meta (workspace) | The header. Its id names the conversation (`finish()` states it, the last header's id, as the file's one name). |
    | `model_change` | Meta (model, provider) | Model context for later responses. |
    | `message`, role `user` | PromptRec | A human turn. String content is one text block, not one per character. |
    | `message`, role `assistant` | ResponseRec, then one ToolCallRec per `toolCall` block | The usage and cost of the message. Thinking is kept as text; a `toolCall` block is the call. |
    | `message`, role `toolResult` | ToolResultRec | Pairs to its call by `toolCallId`. Images are kept as binary-filtered placeholders in the result's `content`. |
    | `message`, role `system` | Ignored (`message:system`) | The harness system prompt, sent as sections; not conversation content. |
    | `message`, role `bashExecution` | Ignored (`message:bashExecution`) | A shell command the user ran from pi; not stored today. |
    | `usage`, kind `cache_warm` | UsageRec when the latest message has the same provider and model the record names (24 of 25 locally); else Ignored (`usage:cache_warm:unattributed`) | Billed tokens with no message behind them. They sum onto the latest response only when that response is the model they were spent on: pi names no response to attach them to, and after a model switch they would be billed to the wrong model. |
    | `usage`, any other kind | Unknown (`usage:<kind>`) | A kind not yet seen; its tokens are counted, not guessed at. |
    | `custom_message` | Ignored (`custom_message:<customType>`) | Harness notices (`workflow-result`, `workflows-progress`, `optmem-wake`, `effort`); not stored today. |
    | `custom` | Ignored (`custom:<customType>`) | Harness state (`advisor-usage` carries model and tokens); not stored today. |
    | `thinking_level_change` | Ignored | A thinking-level setting; not stored today. |
    | `compaction` | Ignored | The summary of compacted context, and the usage of the summarizing call; not stored today. |
    | `context_edit` | Ignored | Marks content removed from the model's context; the content stays stored. |

    Time. Every record with a timestamp states it, so conversation bounds span
    every record as they always have. An ``Ignored`` record carries no time of
    its own, so an ignored record with one is also a ``Meta`` that states only
    that time: the content is Ignored, the time is evidence.

    Usage. pi's ``input`` excludes cache reads on every record: ``totalTokens``
    is ``input + output + cacheRead + cacheWrite`` in all of them, for every
    provider. Input tokens are stored inclusive of cache reads (the uncached
    count is what remains after subtracting them), so the decoder adds the cache
    reads back. ``cacheRead`` and ``cacheWrite`` travel as usage details.

    Identity. The file's conversation is ``pi_agent::<header id>``, and a file
    with no header id is named for its stem. pi's ``parentId`` links entries
    within one file, and every local file is one linear chain, so no lineage is
    stated; a fork would be linearized in file order (no local instance).
    """

    def __init__(self, source: Source):
        self._session_id: str | None = None
        # (provider, model) as the latest assistant message states them: the
        # response a cache-warm usage record would sum onto.
        self._latest: tuple[str | None, str | None] | None = None

    def decode(self, unit: dict) -> list[Record]:
        kind = unit.get("type")
        if not isinstance(kind, str):
            return [Unknown(reason="no type")]
        at = unit.get("timestamp") or None
        if kind == "session":
            self._session_id = unit.get("id") or None
            return [Meta(at=at, workspace_path=unit.get("cwd") or None)]
        if kind == "model_change":
            return [Meta(at=at, model=unit.get("modelId") or None, provider=_provider(unit.get("provider")))]
        if kind == "message":
            return self._message(unit, at)
        if kind == "usage":
            return self._usage_record(unit, at)
        if kind in ("custom_message", "custom"):
            return [*_time(at), Ignored(reason=f"{kind}:{unit.get('customType') or 'unnamed'}")]
        if kind in _IGNORED_TYPES:
            return [*_time(at), Ignored(reason=kind)]
        return [*_time(at), Unknown(reason=f"type:{kind}")]

    def finish(self) -> list[Record]:
        """The file's conversation name: the last header's id, stated once, here."""
        return [Session(external_id=f"{NAME}::{self._session_id}")] if self._session_id else []

    def _message(self, unit: dict, at: str | None) -> list[Record]:
        msg = unit.get("message")
        if not isinstance(msg, dict):
            return [*_time(at), Unknown(reason="message:no-object")]
        role = msg.get("role")
        if role == "user":
            return [PromptRec(at=at, content=[_block(b) for b in _blocks(msg.get("content"))])]
        if role == "assistant":
            self._latest = (msg.get("provider"), msg.get("model"))
            return _assistant(msg, at)
        if role == "toolResult":
            return _tool_result(msg, at)
        if role in _IGNORED_ROLES:
            return [*_time(at), Ignored(reason=f"message:{role}")]
        return [*_time(at), Unknown(reason=f"message:role:{role}")]

    def _usage_record(self, unit: dict, at: str | None) -> list[Record]:
        kind = unit.get("kind")
        if kind != "cache_warm":
            return [*_time(at), Unknown(reason=f"usage:{kind}")]
        usage, details = _usage(unit.get("usage"))
        if usage is None:
            return [*_time(at), Unknown(reason="usage:no-usage")]
        # Attribution is checked, not assumed: the record names the model its
        # tokens were spent on, and the assembler can only sum it onto the latest
        # response. A mismatch (a model switch) is named and kept out of the
        # wrong model's totals.
        named = (unit.get("provider"), unit.get("model"))
        if None in named or named != self._latest:
            return [*_time(at), Ignored(reason="usage:cache_warm:unattributed")]
        return [UsageRec(at=at, usage=usage, usage_details=details)]


def _assistant(msg: dict, at: str | None) -> list[Record]:
    usage, details = _usage(msg.get("usage"))
    attributes: dict[str, str] = {}
    usage_data = msg.get("usage")
    cost = usage_data.get("cost") if isinstance(usage_data, dict) else None
    if isinstance(cost, dict) and cost.get("total") is not None:
        attributes["cost"] = str(cost["total"])

    content: list[ContentBlock] = []
    calls: list[Record] = []
    for block in _blocks(msg.get("content")):
        if isinstance(block, dict) and block.get("type") == "toolCall":
            name = block.get("name", "unknown")
            arguments = parse_json_args(block.get("arguments", {}))
            content.append(ContentBlock(block_type="tool_use", content={"id": block.get("id"), "name": name, "input": arguments}))
            # The call takes the response's place: it has no parent_key, so it attaches to this response.
            calls.append(ToolCallRec(at=at, name=name, input=arguments, call_id=block.get("id") or None))
        elif isinstance(block, dict) and block.get("type") == "thinking":
            content.append(ContentBlock(block_type="thinking", content={"text": block.get("thinking", "")}))
        else:
            content.append(_block(block))
    response = ResponseRec(
        at=at,
        content=content,
        model=msg.get("model") or None,
        provider=_provider(msg.get("provider")),
        usage=usage,
        usage_details=details,
        attributes=attributes,
    )
    return [response, *calls]


def _tool_result(msg: dict, at: str | None) -> list[Record]:
    call_id = msg.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return [*_time(at), Unknown(reason="toolResult:no-toolCallId")]
    texts: list[str] = []
    extra: list = []  # non-text content, binary-filtered, kept under "content"
    for block in _blocks(msg.get("content")):
        if isinstance(block, dict) and block.get("type") == "text":
            if text := block.get("text"):
                texts.append(text)
        elif isinstance(block, dict) and block.get("type") == "image":
            extra.append(filter_binary_block(_image(block)))
        else:
            extra.append(block)
    result: dict = {"output": "\n".join(texts)}
    if extra:
        result["content"] = extra
    return [
        ToolResultRec(
            at=at,
            call_id=call_id,
            result=result,
            status="error" if msg.get("isError", False) else "success",
        )
    ]


def _usage(data) -> tuple[Usage | None, dict[str, int]]:
    """A message's usage as the store keeps it, and its cache counters as usage details.

    ``input_tokens`` is inclusive of cache reads: pi reports it exclusive (see
    :class:`PiDecoder`), so the cache reads are added back.
    """
    if not isinstance(data, dict) or not data:
        return None, {}
    raw_input = _count(data.get("input"))
    cache_read = _count(data.get("cacheRead"))
    cache_write = _count(data.get("cacheWrite"))
    input_tokens = None if raw_input is None else raw_input + (cache_read or 0)
    details: dict[str, int] = {}
    if cache_read is not None:
        details["cache_read_input_tokens"] = cache_read
    if cache_write is not None:
        details["cache_creation_input_tokens"] = cache_write
    return Usage(input_tokens=input_tokens, output_tokens=_count(data.get("output"))), details


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _provider(raw) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return _PROVIDERS.get(raw, raw)


def _time(at: str | None) -> list[Record]:
    """The time of a record that states nothing else: evidence for the conversation's bounds."""
    return [Meta(at=at)] if at else []


def _blocks(content) -> list:
    """Content as a list of blocks. A string is one text block; a list's strings are text blocks too."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [{"type": "text", "text": c} if isinstance(c, str) else c for c in content]
    return []


def _block(block) -> ContentBlock:
    """One content block. Images go through the binary filter; everything else keeps its fields."""
    if not isinstance(block, dict):
        return ContentBlock(block_type="unknown", content={"value": block})
    kind = block.get("type", "unknown")
    if kind == "text":
        return ContentBlock(block_type="text", content={"text": block.get("text", "")})
    if kind == "image":
        return ContentBlock(block_type="image", content=filter_binary_block(_image(block)))
    return ContentBlock(block_type=kind, content=block)


def _image(block: dict) -> dict:
    """pi's image block (``data`` and ``mimeType``) in the Anthropic base64 shape the binary filter reads."""
    data = block.get("data")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": block.get("mimeType"), "data": data if isinstance(data, str) else ""},
    }


read = read_jsonl
decoder = PiDecoder


def default_session(source: Source) -> Session:
    """The conversation of a file that names no session of its own."""
    return Session(external_id=f"{NAME}::{source.as_path.stem}")


def _parse_records(records: list[dict], path: Path) -> list[Conversation]:
    """The conversations ``records`` make, as if read from ``path``. Reads nothing itself.

    The identity preflight parses records it has already validated this way; the
    file reader stays at :func:`read`.
    """
    adapter = DecodingAdapter(sys.modules[__name__])
    return adapter.assemble(Source(kind="file", location=path), units=records).conversations
