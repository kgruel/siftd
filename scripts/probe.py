#!/usr/bin/env python3
"""Conservation probe: what does each built-in adapter drop?

For every built-in adapter, over the sources its ``discover()`` yields, this
measures two things and reports the difference:

- NATIVE: counts computed from the raw files by a small per-format counter in
  ``NATIVE_COUNTERS``. Native counters never call the adapter.
- PARSED: counts from the ``Conversation`` objects that ``parse()`` yields.

Read-only. ``parse()`` is called directly, so no database is opened or written.
The native counters are the only adapter-specific part. Each is kept small on
purpose: the shared disposition ledger is meant to replace them.

Sign convention: ``diff = native - parsed``. Positive means the raw files hold
more than the adapter yielded (dropped). Negative means the adapter yielded more
than the raw files hold (duplicated, or counted per record where the format
counts per message).

Each source is measured once. Sources are deduplicated by resolved path, because
a location that is a symlink to another default location is yielded twice by
``discover()``; the duplicates are reported, not counted.

Usage:
    ./dev probe [--adapter NAME] [--limit N] [--json]
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from siftd.domain import Conversation, Response

# Error messages kept per adapter; counts are always exact.
ERROR_SAMPLE = 5

# Fields compared between native and parsed. Token fields are not comparable
# for formats that record no usage at all, so they are shown as "-".
DIFF_FIELDS = (
    "user",
    "assistant",
    "tool_calls",
    "tool_results",
    "input_tokens",
    "output_tokens",
    "cache_tokens",
)
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_tokens")


@dataclass
class Tally:
    """Counts for one source, or summed over many.

    The shared field names mean the same thing on both sides. ``user`` is a user
    message (native) or a prompt (parsed). ``tool_results`` is a tool result
    (native) or a tool call with a result attached (parsed). ``records`` is
    native only: raw records by top-level type.
    """

    records: Counter[str] = field(default_factory=Counter)
    user: int = 0
    assistant: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    # Parsed-only.
    conversations: int = 0
    conversations_no_prompts: int = 0
    conversations_no_responses: int = 0
    tool_calls_no_result: int = 0

    def add(self, other: Tally) -> None:
        self.records.update(other.records)
        for f in dataclasses.fields(self):
            if f.name != "records":
                setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name != "records"}
        out["records"] = dict(sorted(self.records.items()))
        return out


# ---------------------------------------------------------------------------
# Raw-value helpers
# ---------------------------------------------------------------------------


def _num(value: object) -> int:
    """A token or count field as an int; 0 when absent or not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _blocks(content: object) -> list[dict]:
    return [b for b in _list(content) if isinstance(b, dict)]


def _jsonl(path: Path, tally: Tally) -> Iterator[dict]:
    """Yield object records. Malformed and non-object lines are counted, not hidden."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                tally.records["<malformed>"] += 1
                continue
            if isinstance(record, dict):
                yield record
            else:
                tally.records["<non-object>"] += 1


def _load_json_object(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return _obj(json.load(handle))


def _json_column(raw: object) -> dict:
    if not isinstance(raw, str):
        return {}
    try:
        return _obj(json.loads(raw))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Native counters: one per format. Each returns a Tally from the raw file alone.
# ---------------------------------------------------------------------------


def _native_claude_code(path: Path) -> Tally:
    # One assistant message is split across several streamed records that share
    # message.id. It is one response, and its usage is counted once: the max
    # across its records. Tool uses and results are de-duplicated by id.
    t = Tally()
    message_keys: set[object] = set()
    usage: dict[object, dict[str, int]] = {}
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    for record in _jsonl(path, t):
        kind = str(record.get("type"))
        t.records[kind] += 1
        if kind not in ("user", "assistant"):
            continue
        message = _obj(record.get("message"))
        blocks = _blocks(message.get("content"))
        if kind == "user":
            results = [b for b in blocks if b.get("type") == "tool_result"]
            tool_result_ids.update(str(b["tool_use_id"]) for b in results if b.get("tool_use_id"))
            # A user record is one prompt unless it is only tool results. A mixed
            # record (tool_result plus text) is still a prompt.
            has_prompt_content = any(b.get("type") != "tool_result" for b in blocks)
            if message.get("content") and (not results or has_prompt_content):
                t.user += 1
            continue
        key = message.get("id") or record.get("uuid") or object()
        if key not in message_keys:
            message_keys.add(key)
            t.assistant += 1
        tool_use_ids.update(str(b["id"]) for b in blocks if b.get("type") == "tool_use" and b.get("id"))
        counts = usage.setdefault(key, {})
        raw = _obj(message.get("usage"))
        for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            counts[name] = max(counts.get(name, 0), _num(raw.get(name)))
    t.tool_calls = len(tool_use_ids)
    t.tool_results = len(tool_result_ids)
    for counts in usage.values():
        t.input_tokens += counts.get("input_tokens", 0)
        t.output_tokens += counts.get("output_tokens", 0)
        t.cache_tokens += counts.get("cache_read_input_tokens", 0) + counts.get("cache_creation_input_tokens", 0)
    return t


def _native_codex_cli(path: Path) -> Tally:
    # Usage is the sum of info.last_token_usage over token_count events whose
    # info is non-null and whose cumulative total moved: codex re-emits the
    # previous event unchanged (18,435 times on one corpus), and a re-emission
    # is not another API call. Tool calls and results are the response_item
    # variants.
    t = Tally()
    previous_total = None
    for record in _jsonl(path, t):
        kind = str(record.get("type"))
        t.records[kind] += 1
        payload = _obj(record.get("payload"))
        ptype = payload.get("type")
        if kind == "response_item":
            if ptype == "message":
                if payload.get("role") == "user":
                    t.user += 1
                elif payload.get("role") == "assistant":
                    t.assistant += 1
            elif ptype in ("function_call", "custom_tool_call"):
                t.tool_calls += 1
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                t.tool_results += 1
        elif kind == "event_msg" and ptype == "token_count":
            info = _obj(payload.get("info"))
            total = info.get("total_token_usage")
            if total is not None and total == previous_total:
                continue
            previous_total = total
            last = _obj(info.get("last_token_usage"))
            t.input_tokens += _num(last.get("input_tokens"))
            t.output_tokens += _num(last.get("output_tokens"))
            t.cache_tokens += _num(last.get("cached_input_tokens"))
    return t


# Step types that are the executed result of a tool call. Explicit, from a
# census of the local corpus. Every other step type (ERROR_MESSAGE,
# EPHEMERAL_MESSAGE, SYSTEM_MESSAGE, CHECKPOINT, ...) is not a tool result and
# is counted only under records. A new step type is therefore visible in the
# record inventory but never inflates tool_results by default.
_ANTIGRAVITY_RESULT_STEPS = frozenset(
    {
        "GENERIC",
        "RUN_COMMAND",
        "VIEW_FILE",
        "CODE_ACTION",
        "GREP_SEARCH",
        "LIST_DIRECTORY",
        "SEARCH_WEB",
        "READ_URL_CONTENT",
    }
)


def _native_antigravity_cli(path: Path) -> Tally:
    # Usage lives only on PLANNER_RESPONSE steps, as top-level token fields.
    t = Tally()
    for step in _jsonl(path, t):
        kind = str(step.get("type"))
        t.records[kind] += 1
        if kind == "USER_INPUT":
            t.user += 1
        elif kind == "PLANNER_RESPONSE":
            t.assistant += 1
            t.tool_calls += len(_list(step.get("tool_calls")))
            # Native input excludes cache reads; siftd stores the google-source
            # convention (inclusive), so compare like with like.
            cache_read = _num(step.get("cache_read_tokens"))
            t.input_tokens += _num(step.get("input_tokens")) + cache_read
            t.output_tokens += _num(step.get("output_tokens"))
            t.cache_tokens += cache_read
        elif kind in _ANTIGRAVITY_RESULT_STEPS:
            t.tool_results += 1
    return t


def _native_gemini_cli(path: Path) -> Tally:
    # One JSON document; the messages array holds user, gemini, info and error.
    # A tool call has a result when its result list is non-empty.
    t = Tally()
    for message in _list(_load_json_object(path).get("messages")):
        if not isinstance(message, dict):
            continue
        kind = str(message.get("type"))
        t.records[kind] += 1
        if kind == "user":
            t.user += 1
        elif kind == "gemini":
            t.assistant += 1
            calls = [c for c in _list(message.get("toolCalls")) if isinstance(c, dict)]
            t.tool_calls += len(calls)
            t.tool_results += sum(1 for c in calls if c.get("result"))
            tokens = _obj(message.get("tokens"))
            t.input_tokens += _num(tokens.get("input"))
            t.output_tokens += _num(tokens.get("output"))
            t.cache_tokens += _num(tokens.get("cached"))
    return t


def _replay_vscode_session(path: Path, t: Tally) -> dict:
    """Whole-file JSON, or a JSON-lines patch log replayed onto its kind-0 root.

    The replay is re-derived here (kind 0 sets the root, kind 1 sets a path,
    kind 2 appends a list at a path) rather than reused from the adapter, so
    these counts do not inherit the adapter's reading of the log.
    """
    if path.suffix != ".jsonl":
        return _load_json_object(path)
    root: dict = {}
    for op in _jsonl(path, t):
        kind, keys, value = op.get("kind"), op.get("k"), op.get("v")
        if kind == 0 and isinstance(value, dict):
            root = value
            continue
        if kind not in (1, 2) or not isinstance(keys, list) or not keys:
            continue
        try:
            parent: Any = root
            for key in keys[:-1]:
                parent = parent[key]
            if kind == 1:
                parent[keys[-1]] = value
            elif isinstance(parent[keys[-1]], list) and isinstance(value, list):
                parent[keys[-1]].extend(value)
        except (KeyError, IndexError, TypeError):
            continue
    return root


def _native_vscode(path: Path) -> Tally:
    # A request is one user message and, if it has parts, one assistant
    # message. Tool invocations are de-duplicated by toolCallId, because a patch
    # log can re-state a part. Usage is request.result.usage.
    t = Tally()
    session = _replay_vscode_session(path, t)
    tool_ids: set[object] = set()
    complete_ids: set[object] = set()
    for request in _list(session.get("requests")):
        if not isinstance(request, dict):
            continue
        t.records["request"] += 1
        message = request.get("message")
        if isinstance(message, dict):
            message = message.get("text")
        if message:
            t.user += 1
        parts = [p for p in _list(request.get("response")) if isinstance(p, dict)]
        if parts:
            t.assistant += 1
        for part in parts:
            kind = str(part.get("kind") or "")
            t.records[f"part:{kind}"] += 1
            if kind == "toolInvocationSerialized":
                key = part.get("toolCallId") or object()
                tool_ids.add(key)
                if part.get("isComplete") is True:
                    complete_ids.add(key)
        usage = _obj(_obj(request.get("result")).get("usage"))
        t.input_tokens += _num(usage.get("promptTokens"))
        t.output_tokens += _num(usage.get("completionTokens"))
    t.tool_calls = len(tool_ids)
    t.tool_results = len(complete_ids)
    return t


def _native_opencode(path: Path) -> Tally:
    # SQLite, opened read-only. Messages carry the role and, for assistants,
    # token counts. Tool parts are tool calls; completed or errored ones have a
    # result.
    t = Tally()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table in ("session", "message", "part"):
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            t.records[table] = count
        for (raw,) in conn.execute("SELECT data FROM message"):
            data = _json_column(raw)
            if data.get("role") == "user":
                t.user += 1
            elif data.get("role") == "assistant":
                t.assistant += 1
                tokens = _obj(data.get("tokens"))
                cache = _obj(tokens.get("cache"))
                t.input_tokens += _num(tokens.get("input"))
                t.output_tokens += _num(tokens.get("output"))
                t.cache_tokens += _num(cache.get("read")) + _num(cache.get("write"))
        for (raw,) in conn.execute("SELECT data FROM part"):
            data = _json_column(raw)
            if data.get("type") == "tool":
                t.tool_calls += 1
                if _obj(data.get("state")).get("status") in ("completed", "error"):
                    t.tool_results += 1
    finally:
        conn.close()
    return t


def _native_pi_agent(path: Path) -> Tally:
    # Message records carry role user, assistant or toolResult. Usage and
    # tool calls live on assistant messages. A standalone "usage" record (cache
    # warming, kind cache_warm) is billed tokens with no message behind it: its
    # tokens are summed, and it is not an assistant message.
    # pi's input excludes cache reads (totalTokens is input + output + cacheRead
    # + cacheWrite on every record), so the native input adds cacheRead back,
    # as siftd stores it: the same normalization the antigravity counter makes.
    t = Tally()
    for record in _jsonl(path, t):
        kind = str(record.get("type"))
        t.records[kind] += 1
        if kind == "usage":
            usage = _obj(record.get("usage"))
            t.input_tokens += _num(usage.get("input")) + _num(usage.get("cacheRead"))
            t.output_tokens += _num(usage.get("output"))
            t.cache_tokens += _num(usage.get("cacheRead")) + _num(usage.get("cacheWrite"))
            continue
        if kind != "message":
            continue
        message = _obj(record.get("message"))
        role = message.get("role")
        if role == "user":
            t.user += 1
        elif role == "assistant":
            t.assistant += 1
            t.tool_calls += sum(1 for b in _blocks(message.get("content")) if b.get("type") == "toolCall")
            usage = _obj(message.get("usage"))
            t.input_tokens += _num(usage.get("input")) + _num(usage.get("cacheRead"))
            t.output_tokens += _num(usage.get("output"))
            t.cache_tokens += _num(usage.get("cacheRead")) + _num(usage.get("cacheWrite"))
        elif role == "toolResult":
            t.tool_results += 1
    return t


def _native_copilot_cli(path: Path) -> Tally:
    # Event log. Tool requests ride on assistant.message; a completed tool
    # execution is a result. The format records no token usage.
    t = Tally()
    for event in _jsonl(path, t):
        kind = str(event.get("type"))
        t.records[kind] += 1
        data = _obj(event.get("data"))
        if kind == "user.message":
            t.user += 1
        elif kind == "assistant.message":
            t.assistant += 1
            t.tool_calls += len(_list(data.get("toolRequests")))
        elif kind == "tool.execution_complete":
            t.tool_results += 1
    return t


_AIDER_SESSION = "# aider chat started at"
_AIDER_TOKENS = re.compile(r"Tokens:\s*([\d.,]+[kKmM]?)\s*sent,\s*([\d.,]+[kKmM]?)\s*received\.")
_AIDER_SCALE = {"": 1, "k": 1_000, "m": 1_000_000}


def _aider_token_count(text: str) -> int:
    """'4.5k' -> 4500, '1,200' -> 1200. 0 when the text is not a number."""
    match = re.fullmatch(r"([\d.,]+)([kKmM]?)", text)
    if match is None:
        return 0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    return round(value * _AIDER_SCALE[match.group(2).lower()])


def _native_aider(path: Path) -> Tally:
    # Markdown constructs: "#### " lines are user prompts, "> " lines are tool
    # output, any other line is assistant text. A run of one kind is one block.
    # A whitespace-only assistant run is no block. Tool output is not modelled
    # as ToolCall, so it is counted under records, not tool_results.
    t = Tally()
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    kinded: list[tuple[str, str]] = []
    for line in lines:
        if line.startswith(_AIDER_SESSION):
            kinded.append(("session", line))
        elif line.startswith("#### "):
            kinded.append(("user", line))
        elif line.startswith("> "):
            kinded.append(("tool", line))
        else:
            kinded.append(("assistant", line))
    for kind, group in itertools.groupby(kinded, key=lambda pair: pair[0]):
        group_lines = [line for _, line in group]
        if kind == "session":
            t.records["session"] += len(group_lines)
        elif kind == "user":
            t.user += 1
        elif kind == "tool":
            t.records["tool_block"] += 1
        elif any(line.strip() for line in group_lines):
            t.assistant += 1
    for line in lines:
        match = _AIDER_TOKENS.search(line)
        if match:
            t.input_tokens += _aider_token_count(match.group(1))
            t.output_tokens += _aider_token_count(match.group(2))
    return t


class NativeCounter(NamedTuple):
    count: Callable[[Path], Tally]
    records_usage: bool  # does the format record token usage at all?


NATIVE_COUNTERS: dict[str, NativeCounter] = {
    "claude_code": NativeCounter(_native_claude_code, True),
    "codex_cli": NativeCounter(_native_codex_cli, True),
    "antigravity_cli": NativeCounter(_native_antigravity_cli, True),
    "gemini_cli": NativeCounter(_native_gemini_cli, True),
    "vscode": NativeCounter(_native_vscode, True),
    "pi_agent": NativeCounter(_native_pi_agent, True),
    "copilot_cli": NativeCounter(_native_copilot_cli, False),
    "opencode": NativeCounter(_native_opencode, True),
    "aider": NativeCounter(_native_aider, True),
}


# ---------------------------------------------------------------------------
# Parsed side: the same fields, read from Conversation objects.
# ---------------------------------------------------------------------------


def _attr_num(attributes: dict[str, str], key: str) -> int:
    try:
        return int(attributes.get(key, "0"))
    except ValueError:
        return 0


def _add_parsed_usage(t: Tally, response: Response) -> None:
    usage = response.usage
    if usage is not None:
        t.input_tokens += usage.input_tokens or 0
        t.output_tokens += usage.output_tokens or 0
    t.cache_tokens += _attr_num(response.attributes, "cache_read_input_tokens")
    t.cache_tokens += _attr_num(response.attributes, "cache_creation_input_tokens")


def _add_parsed_response(t: Tally, response: Response) -> None:
    t.assistant += 1
    _add_parsed_usage(t, response)
    for call in response.tool_calls:
        t.tool_calls += 1
        if call.result is None:
            t.tool_calls_no_result += 1
        else:
            t.tool_results += 1


def parsed_tally(conversations: Iterable[Conversation]) -> Tally:
    t = Tally()
    for conversation in conversations:
        t.conversations += 1
        if not conversation.prompts:
            t.conversations_no_prompts += 1
        responses = 0
        # Responses before the first prompt are stored in the preamble; they are
        # parsed output like any other, so they are counted here too.
        for response in conversation.preamble:
            responses += 1
            _add_parsed_response(t, response)
        for prompt in conversation.prompts:
            t.user += 1
            for response in prompt.responses:
                responses += 1
                _add_parsed_response(t, response)
        if responses == 0:
            t.conversations_no_responses += 1
    return t


# ---------------------------------------------------------------------------
# Measurement and reporting
# ---------------------------------------------------------------------------


@dataclass
class AdapterReport:
    name: str
    records_usage: bool
    sources: int = 0
    duplicate_yields: int = 0
    native_errors: int = 0
    parse_errors: int = 0
    error_samples: list[str] = field(default_factory=list)
    native: Tally = field(default_factory=Tally)
    parsed: Tally = field(default_factory=Tally)

    def diff(self) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for name in DIFF_FIELDS:
            if name in TOKEN_FIELDS and not self.records_usage:
                out[name] = None
            else:
                out[name] = getattr(self.native, name) - getattr(self.parsed, name)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "duplicate_yields": self.duplicate_yields,
            "native_errors": self.native_errors,
            "parse_errors": self.parse_errors,
            "error_samples": self.error_samples,
            "records_usage": self.records_usage,
            "native": self.native.to_dict(),
            "parsed": self.parsed.to_dict(),
            "diff": self.diff(),
        }


def _note_error(report: AdapterReport, side: str, path: Path, exc: Exception) -> None:
    if side == "native":
        report.native_errors += 1
    else:
        report.parse_errors += 1
    if len(report.error_samples) < ERROR_SAMPLE:
        report.error_samples.append(f"{side} {path}: {exc!r}"[:300])


def measure_adapter(name: str, module: Any, *, limit: int | None = None) -> AdapterReport:
    counter = NATIVE_COUNTERS[name]
    report = AdapterReport(name=name, records_usage=counter.records_usage)
    seen: set[Path] = set()
    for source in module.discover():
        path = Path(source.location)
        key = path.resolve()
        if key in seen:
            report.duplicate_yields += 1
            continue
        if limit is not None and report.sources >= limit:
            break
        seen.add(key)
        report.sources += 1

        native = Tally()
        try:
            native = counter.count(path)
        except Exception as exc:  # a probe records what failed; it never stops the run
            _note_error(report, "native", path, exc)

        parsed = Tally()
        try:
            parsed = parsed_tally(list(module.parse(source)))
        except Exception as exc:
            _note_error(report, "parse", path, exc)

        report.native.add(native)
        report.parsed.add(parsed)
    return report


_COLUMNS = (
    ("user", "user/prompt"),
    ("assistant", "asst/resp"),
    ("tool_calls", "tool calls"),
    ("tool_results", "results"),
    ("input_tokens", "input tok"),
    ("output_tokens", "output tok"),
    ("cache_tokens", "cache tok"),
)


def _fmt(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) if i == 0 else h.rjust(w) for i, (h, w) in enumerate(zip(headers, widths)))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths))))
    return "\n".join(lines)


def _tally_cells(report: AdapterReport, tally: Tally) -> list[str]:
    """Metric cells for one tally; token cells are '-' where the format records no usage."""
    return [
        "-" if key in TOKEN_FIELDS and not report.records_usage else f"{getattr(tally, key):,}" for key, _ in _COLUMNS
    ]


def render_text(reports: list[AdapterReport]) -> str:
    header = ["adapter", "sources", "dup", "errors"] + [label for _, label in _COLUMNS]

    def rows(cells) -> list[list[str]]:
        out = []
        for r in reports:
            base = [r.name, f"{r.sources:,}", f"{r.duplicate_yields:,}", f"{r.native_errors}/{r.parse_errors}"]
            out.append(base + cells(r))
        return out

    diff_rows = rows(lambda r: [_fmt(r.diff()[key]) for key, _ in _COLUMNS])
    native_rows = rows(lambda r: _tally_cells(r, r.native))
    parsed_rows = rows(lambda r: _tally_cells(r, r.parsed))
    parsed_only_headers = ["adapter", "convs", "no-prompt convs", "no-response convs", "calls w/o result"]
    parsed_only_rows = [
        [
            r.name,
            f"{r.parsed.conversations:,}",
            f"{r.parsed.conversations_no_prompts:,}",
            f"{r.parsed.conversations_no_responses:,}",
            f"{r.parsed.tool_calls_no_result:,}",
        ]
        for r in reports
    ]
    sections = [
        "DIFF  native - parsed  (+ dropped by adapter, - duplicated or over-counted)",
        _table(header, diff_rows),
        "",
        "NATIVE  counted from raw files",
        _table(header, native_rows),
        "",
        "PARSED  counted from parse() output",
        _table(header, parsed_rows),
        "",
        "PARSED ONLY",
        _table(parsed_only_headers, parsed_only_rows),
    ]
    samples = [(r.name, s) for r in reports for s in r.error_samples]
    if samples:
        sections += ["", "ERROR SAMPLES"] + [f"  {name}: {msg}" for name, msg in samples]
    sections += [
        "",
        "dup = sources yielded again under a second location (same resolved path); not measured.",
        "'-' = the format records no token usage, so there is nothing to compare.",
    ]
    return "\n".join(sections)


def render_json(reports: list[AdapterReport]) -> str:
    # The corpus is live (the session writing this is one of its files), so a
    # snapshot is only comparable to others when its time is recorded.
    payload = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "adapters": {r.name: r.to_dict() for r in reports},
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="./dev probe",
        description="Conservation probe: native raw-file counts vs adapter parse() output, per adapter. Read-only.",
    )
    parser.add_argument("--adapter", help="measure only this built-in adapter")
    parser.add_argument("--limit", type=int, help="measure at most N unique source files per adapter")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.adapter is not None and args.adapter not in NATIVE_COUNTERS:
        parser.error(f"unknown adapter {args.adapter!r}; choose from {', '.join(sorted(NATIVE_COUNTERS))}")

    from siftd.adapters.registry import load_builtin_adapters

    modules = {info.name: info.module for info in load_builtin_adapters()}
    missing = sorted(set(modules) - set(NATIVE_COUNTERS))
    if missing:
        parser.error(f"no native counter for built-in adapter(s): {', '.join(missing)}")

    selected = [args.adapter] if args.adapter else sorted(NATIVE_COUNTERS)
    reports = [measure_adapter(name, modules[name], limit=args.limit) for name in selected]
    print(render_json(reports) if args.json else render_text(reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
