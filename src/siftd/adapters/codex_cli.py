"""Codex CLI adapter for siftd.

Pure parser: reads JSONL session files and yields Conversation domain objects.
No storage coupling. The adapter only decodes: :class:`CodexDecoder` turns each
JSONL line into canonical records, and the shared assembler
(:mod:`siftd.adapters.assembly`) decides where they attach.
"""

from collections.abc import Iterable
from pathlib import Path

from siftd.adapters._jsonl import parse_block
from siftd.adapters.assembly import assemble, read_jsonl
from siftd.adapters.peek_projection import make_peek_hooks_from_decoder
from siftd.adapters.records import (
    Ignored,
    LineageRec,
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
from siftd.adapters.sdk import build_harness, discover_files
from siftd.domain import ContentBlock, Conversation, Source, Usage
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

# Harness-authored user messages: context the harness sends the model, not text a
# person typed. A user message is injected when its first text block opens with one
# of these markers (the marker is kept as the block's ``meta_source``). Corpus: 1,887
# of 4,232 user messages, 44.6%. Automated prompts (``# Task``, review briefs) are
# left as text: a person or a skill wrote them, and they are not harness context.
_INJECTED_MARKERS: dict[str, str] = {
    "# AGENTS.md instructions": "AGENTS.md",
    "<environment_context>": "environment_context",
    "<recommended_plugins>": "recommended_plugins",
    "<user_action>": "user_action",
    "<subagent_notification>": "subagent_notification",
    "<turn_aborted>": "turn_aborted",
}

# Usage a token_count reports under attributes, not as first-class Usage fields.
# Both cache names carry the cached-input count, as they always have.
_CACHE_ATTRIBUTE_KEYS = ("cached_input_tokens", "cache_read_input_tokens")

# Duration keys a task_complete carries, stored on the latest response.
_TASK_DURATION_KEYS = ("duration_ms", "time_to_first_token_ms")

# Record types that are not conversation content. Each is Ignored by name; the
# docstring of CodexDecoder says why, and tests/adapters/test_codex_cli.py keeps
# that table and these sets equal.
_IGNORED_TOP_TYPES = frozenset({
    "token_usage_record",
    "inter_agent_communication_metadata",
    "world_state",
    "compacted",
})
_IGNORED_ITEM_TYPES = frozenset({
    "web_search_call",
    "agent_message",
    "ghost_snapshot",
    "tool_search_call",
    "tool_search_output",
})
_IGNORED_EVENT_TYPES = frozenset({
    "item_completed",
    "agent_reasoning",
    "agent_message",
    "user_message",
    "task_started",
    "exec_command_end",
    "patch_apply_end",
    "web_search_end",
    "thread_settings_applied",
    "entered_review_mode",
    "exited_review_mode",
    "sub_agent_activity",
    "turn_aborted",
    "context_compacted",
    "mcp_tool_call_end",
    "thread_rolled_back",
})


HARNESS = build_harness(NAME, HARNESS_SOURCE, HARNESS_LOG_FORMAT, HARNESS_DISPLAY_NAME)


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


class CodexDecoder:
    """Turns each Codex CLI JSONL line into canonical records.

    Every top-level type, response item type and event type in the corpus (about
    430k lines across 1,340 files) is classified here; one this table does not
    name is ``Unknown``. A decoder holds only decoding state: the first session
    meta, the time bounds, whether a response has opened, and the last cumulative
    token total.

    | Record | Becomes | Why |
    |---|---|---|
    | `session_meta` | Meta (workspace), and LineageRec for a subagent and/or a fork | The file's own session: the first one names it. A later one is Ignored (`session_meta/restated`), because a forked file restates its parent's meta. |
    | `turn_context` | Meta (model), or Ignored (`turn_context/no-model`) | The model in effect for a turn. A later Meta replaces it, so each response carries the model of its own turn. |
    | `token_usage_record` | Ignored | A per-file usage summary; the per-call `token_count` events are the usage. |
    | `inter_agent_communication_metadata` | Ignored | Routing metadata for messages between agents; the messages are `response_item/agent_message`. |
    | `world_state` | Ignored | An environment snapshot (sandbox, agents instructions), not conversation content. |
    | `compacted` | Ignored | A compaction record carrying the replaced history. |
    | `response_item/message:user` | PromptRec | A human turn. Injected harness context (see `_INJECTED_MARKERS`) stays a prompt with `meta_text` blocks, so it never reaches user-text search. |
    | `response_item/message:assistant` | ResponseRec, keyed by the item id when stated | A response of its own. Text before any user message is the preamble, not a synthesized prompt. |
    | `response_item/message:developer` | Ignored (`response_item/message:developer`) | Permissions, skill and multi-agent instructions the harness gives the model. |
    | `response_item/reasoning` | ResponseRec (thinking blocks, keyed by the item id when stated), or Ignored (`response_item/reasoning:no-summary`) | Only the summary is renderable; the encrypted content is opaque. Most items have an empty summary. |
    | `response_item/function_call` | ResponseRec (tool_use block) and ToolCallRec, both keyed by the item id when stated | A response of its own, holding the call. Arguments are JSON; a non-object is kept as `{"raw": ...}`. |
    | `response_item/function_call_output` | ToolResultRec | Pairs with its call by `call_id`, in either order. |
    | `response_item/custom_tool_call` | ResponseRec (tool_use block) and ToolCallRec, both keyed by the item id when stated | As `function_call`; its input is a raw string, kept as `{"raw": ...}`. |
    | `response_item/custom_tool_call_output` | ToolResultRec | As `function_call_output`. |
    | `response_item/web_search_call` | Ignored | A web search without a result record; not stored today. |
    | `response_item/agent_message` | Ignored | An inter-agent message (author, recipient, content); not stored today. |
    | `response_item/ghost_snapshot` | Ignored | A workspace snapshot for undo. |
    | `response_item/tool_search_call` | Ignored | A tool lookup; not stored today. |
    | `response_item/tool_search_output` | Ignored | The result of a tool lookup; not stored today. |
    | `event_msg/token_count` | UsageRec, or Ignored (`event_msg/token_count:no-info`, `:no-last-usage`, `:re-emitted`) | One API call's `last_token_usage`. Codex re-logs the previous event with the same cumulative total; a total that did not move is not a new call. |
    | `event_msg/task_complete` | PatchRec on the latest response, or Ignored (`event_msg/task_complete:no-response`, `:no-duration`) | The turn's durations. One per turn, so the later one wins. |
    | `event_msg/user_message` | Ignored | The same text as the `response_item/message:user` it echoes. |
    | `event_msg/agent_message` | Ignored | An echo of an assistant `response_item`. |
    | `event_msg/agent_reasoning` | Ignored | An echo of reasoning summaries; some texts appear nowhere else and are not stored today. |
    | `event_msg/item_completed` | Ignored | An echo of the items in item-era files. |
    | `event_msg/task_started` | Ignored | The start of a turn. |
    | `event_msg/exec_command_end` | Ignored | The end of a command; its output is the `function_call_output`. |
    | `event_msg/patch_apply_end` | Ignored | The end of a patch application. |
    | `event_msg/web_search_end` | Ignored | The end of a web search. |
    | `event_msg/thread_settings_applied` | Ignored | A settings change; not conversation content. |
    | `event_msg/entered_review_mode` | Ignored | A UI mode change. |
    | `event_msg/exited_review_mode` | Ignored | A UI mode change. |
    | `event_msg/sub_agent_activity` | Ignored | A sub-agent's status; the sub-agent is its own file. |
    | `event_msg/turn_aborted` | Ignored | A turn the person stopped. |
    | `event_msg/context_compacted` | Ignored | A compaction marker. |
    | `event_msg/mcp_tool_call_end` | Ignored | The end of an MCP tool call, whose call is a `function_call`. |
    | `event_msg/thread_rolled_back` | Ignored | A rollback marker. |

    Identity. The first ``session_meta.id`` names the conversation:
    ``codex_cli::<id>``. A forked file restates its parent's meta after its own,
    so the last id is not the file's own (the earlier last-wins rule gave 35 child
    files their parent's name). A file with no id is named for its stem. When a
    file restated a different id, that id is stated as superseded, so ingest can
    adopt a row a previous release stored under it. A subagent's lineage names the
    parent it ran in (``thread_spawn.parent_thread_id``, or the top-level
    ``parent_thread_id``); a fork's lineage names ``forked_from_id``. Both are kept:
    they are different relations.

    Responses. A response item (a message, a reasoning summary, a call) is one
    response, keyed by the item's own ``id`` where the file states one. Not every
    item states one, in either era of the files: the items that state none are
    unkeyed. The decoder never groups items:
    no field says which model call an item belongs to, so a grouping would be a
    guess. A call attaches to the response its item opened.

    Usage. Each counted ``token_count`` is one increment that attaches to the
    latest response, or waits for the next one. The assembler sums the increments.
    A ``token_count`` follows the items of its model call, so the increment lands on
    the last item of that call.

    Time. The bounds are the first and last timestamps of every line, as they have
    always been, including lines that create no event.
    """

    def __init__(self, source: Source):
        self._path = source.as_path
        self._units = 0
        self._first_at: str | None = None
        self._last_at: str | None = None
        self._session_id: str | None = None  # the first session_meta's id
        self._restated_id: str | None = None  # the last session_meta's id, when it differs
        self._stated_session = False  # a session_meta has been seen
        self._cwd: str | None = None
        self._responded = False  # any response item has been decoded
        self._last_total: object = None  # cumulative total of the last counted token_count

    def decode(self, unit: dict) -> list[Record]:
        self._units += 1
        at = unit.get("timestamp") or None
        if at is not None:
            self._first_at = at if self._first_at is None or at < self._first_at else self._first_at
            self._last_at = at if self._last_at is None or at > self._last_at else self._last_at
        kind = unit.get("type")
        if not isinstance(kind, str):
            return [Unknown(reason="no type")]
        if kind == "session_meta":
            return self._session_meta(unit, at)
        if kind == "turn_context":
            return self._turn_context(unit, at)
        if kind in _IGNORED_TOP_TYPES:
            return [Ignored(reason=kind)]
        payload = unit.get("payload")
        if not isinstance(payload, dict):
            return [Unknown(reason=f"{kind}:no-payload")]
        if kind == "response_item":
            return self._response_item(payload, at)
        if kind == "event_msg":
            return self._event(payload, at)
        return [Unknown(reason=f"type:{kind}")]

    def finish(self) -> list[Record]:
        """The conversation's name and bounds, which only the whole file settles.

        Stated once, here: the name is the first session_meta's id, and the bounds
        span every line, so they are known only at the end.
        """
        if not self._units:
            return []
        superseded = ()
        if self._restated_id is not None and self._restated_id != self._session_id:
            superseded = (f"{NAME}::{self._restated_id}",)
        return [
            Session(
                external_id=self._external_id(),
                started_at=self._first_at,
                ended_at=self._last_at,
                superseded_external_ids=superseded,
            )
        ]

    def _external_id(self) -> str:
        return f"{NAME}::{self._session_id or self._path.stem}"

    # -- session and context --

    def _session_meta(self, unit: dict, at: str | None) -> list[Record]:
        payload = unit.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        session_id = payload.get("id") or None
        if self._stated_session:
            self._restated_id = session_id
            return [Ignored(reason="session_meta/restated")]
        self._stated_session = True
        self._session_id = session_id
        self._cwd = payload.get("cwd") or None
        out: list[Record] = [Meta(at=at, workspace_path=self._cwd)]
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        if subagent is not None:
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            parent = (spawn.get("parent_thread_id") if isinstance(spawn, dict) else None) or payload.get(
                "parent_thread_id"
            )
            if parent:
                out.append(LineageRec(at=None, kind="subagent", parent=f"{NAME}::{parent}"))
        forked = payload.get("forked_from_id")
        if forked:
            out.append(LineageRec(at=None, kind="fork", parent=f"{NAME}::{forked}"))
        return out

    def _turn_context(self, unit: dict, at: str | None) -> list[Record]:
        payload = unit.get("payload")
        model = payload.get("model") if isinstance(payload, dict) else None
        if not model:
            return [Ignored(reason="turn_context/no-model")]
        return [Meta(at=at, model=str(model))]

    # -- response items --

    def _response_item(self, payload: dict, at: str | None) -> list[Record]:
        item = payload.get("type")
        if item == "message":
            return self._message(payload, at)
        if item == "reasoning":
            texts = _reasoning_summary_texts(payload)
            if not texts:
                return [Ignored(reason="response_item/reasoning:no-summary")]
            blocks = [ContentBlock(block_type="thinking", content={"thinking": text}) for text in texts]
            return [self._response(at, _item_key(payload), blocks)]
        if item in ("function_call", "custom_tool_call"):
            return self._tool_call(payload, at, item == "custom_tool_call")
        if item in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            if not call_id:
                return [Unknown(reason=f"response_item/{item}:no-call_id")]
            return [ToolResultRec(at=at, call_id=call_id, result={"output": payload.get("output", "")})]
        if item in _IGNORED_ITEM_TYPES:
            return [Ignored(reason=f"response_item/{item}")]
        return [Unknown(reason=f"response_item:{item}")]

    def _message(self, payload: dict, at: str | None) -> list[Record]:
        role = payload.get("role")
        if role == "user":
            content = _content(payload.get("content"))
            injected = _injected_marker(content)
            if injected is not None:
                content = [_as_meta_text(block, injected) for block in content]
            return [PromptRec(at=at, content=content)]
        if role == "assistant":
            return [self._response(at, _item_key(payload), _content(payload.get("content")))]
        if role == "developer":
            return [Ignored(reason="response_item/message:developer")]
        return [Unknown(reason=f"response_item/message:role:{role}")]

    def _tool_call(self, payload: dict, at: str | None, custom: bool) -> list[Record]:
        name = payload.get("name", "unknown")
        call_id = payload.get("call_id") or None
        if custom:
            raw = payload.get("input", "")
            tool_input = raw if isinstance(raw, dict) else {"raw": raw}
            block_input = raw
        else:
            tool_input = parse_json_args(payload.get("arguments", "{}"))
            block_input = tool_input
        key = _item_key(payload)
        block = ContentBlock(block_type="tool_use", content={"id": call_id, "name": name, "input": block_input})
        return [
            self._response(at, key, [block]),
            ToolCallRec(at=at, name=name, input=tool_input, call_id=call_id, parent_key=key),
        ]

    def _response(self, at: str | None, key: str | None, content: list[ContentBlock]) -> ResponseRec:
        """One response item. Unkeyed when the file states no id: the assembler opens it as its own."""
        self._responded = True
        return ResponseRec(at=at, key=key, content=content)

    # -- events --

    def _event(self, payload: dict, at: str | None) -> list[Record]:
        event = payload.get("type")
        if event == "token_count":
            return self._token_count(payload, at)
        if event == "task_complete":
            if not self._responded:
                return [Ignored(reason="event_msg/task_complete:no-response")]
            attributes = {key: str(payload[key]) for key in _TASK_DURATION_KEYS if payload.get(key) is not None}
            if not attributes:
                return [Ignored(reason="event_msg/task_complete:no-duration")]
            return [PatchRec(at=at, attributes=attributes)]
        if event in _IGNORED_EVENT_TYPES:
            return [Ignored(reason=f"event_msg/{event}")]
        return [Unknown(reason=f"event_msg:{event}")]

    def _token_count(self, payload: dict, at: str | None) -> list[Record]:
        info = payload.get("info")
        if not isinstance(info, dict) or not info:
            return [Ignored(reason="event_msg/token_count:no-info")]
        last = info.get("last_token_usage")
        if not isinstance(last, dict) or not last:
            # A total with no call behind it is not a call, so it does not move the watermark.
            return [Ignored(reason="event_msg/token_count:no-last-usage")]
        total = info.get("total_token_usage")
        if total is not None:
            if total == self._last_total:
                return [Ignored(reason="event_msg/token_count:re-emitted")]
            self._last_total = total
        input_tokens = last.get("input_tokens")
        output_tokens = last.get("output_tokens")
        if input_tokens is None and output_tokens is None:
            return [Ignored(reason="event_msg/token_count:no-token-counts")]
        details: dict[str, int] = {}
        cached = last.get("cached_input_tokens")
        if cached is not None:
            for key in _CACHE_ATTRIBUTE_KEYS:
                details[key] = int(cached)
        reasoning = last.get("reasoning_output_tokens")
        if reasoning is not None:
            details["reasoning_output_tokens"] = int(reasoning)
        return [
            UsageRec(
                at=at,
                usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
                usage_details=details,
            )
        ]


def _item_key(payload: dict) -> str | None:
    """The response item's own id, the source's identity for it; None when the file states none."""
    value = payload.get("id")
    return value if isinstance(value, str) and value else None


def _content(blocks) -> list[ContentBlock]:
    """Content blocks of a message: text (either direction) and everything else as parsed."""
    if isinstance(blocks, str):
        return [ContentBlock(block_type="text", content={"text": blocks})]
    if not isinstance(blocks, list):
        return []
    out: list[ContentBlock] = []
    for block in blocks:
        if isinstance(block, str):
            out.append(ContentBlock(block_type="text", content={"text": block}))
        elif isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
            out.append(ContentBlock(block_type="text", content={"text": block.get("text", "")}))
        elif isinstance(block, dict):
            # Binary content (an input_image's base64) is filtered here, as the other adapters do.
            out.append(parse_block(block))
        else:
            out.append(ContentBlock(block_type="unknown", content={"value": block}))
    return out


def _injected_marker(content: list[ContentBlock]) -> str | None:
    """The harness marker a user message opens with, or None when a person wrote it.

    The first text block decides. A message whose text does not open with a marker
    is the person's, whatever else it carries.
    """
    for block in content:
        if block.block_type == "text":
            text = str(block.content.get("text", "")).lstrip()
            for prefix, marker in _INJECTED_MARKERS.items():
                if text.startswith(prefix):
                    return marker
            return None
    return None


def _as_meta_text(block: ContentBlock, marker: str) -> ContentBlock:
    """Move an injected block's text off the indexed ``text`` key onto ``meta_text``."""
    if block.block_type != "text":
        return block
    fields = dict(block.content)
    fields["meta_text"] = fields.pop("text", "")
    fields["meta_source"] = marker
    return ContentBlock(block_type="meta_text", content=fields)


def _reasoning_summary_texts(payload: dict) -> list[str]:
    """Extract non-empty summary texts from a reasoning response_item.

    Codex reasoning items carry encrypted_content (opaque) plus a summary
    list of {"type": "summary_text", "text": ...}. Most summaries are empty;
    the non-empty ones are kept as thinking blocks.
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


def _default_session(path: Path) -> Session:
    """The conversation of a file that names no session of its own."""
    return Session(external_id=f"{NAME}::{path.stem}")


def parse(source: Source) -> Iterable[Conversation]:
    """Parse a Codex CLI JSONL file and yield Conversation objects."""
    return assemble(
        source,
        read=read_jsonl,
        decoder=CodexDecoder,
        harness=HARNESS,
        default_session=_default_session(source.as_path),
    ).conversations


# Peek hooks: the same decoder as ingest, assembled and projected, so peek and
# ingest classify each record once.
peek_scan, peek_exchanges, peek_tail = make_peek_hooks_from_decoder(
    read_jsonl,
    CodexDecoder,
    HARNESS,
    default_session_for=_default_session,
    tool_aliases=TOOL_ALIASES,
)
