"""Follow mode: tail a live session file and render turns as they arrive.

This module handles parsing and the poll loop. Rendering is done by
callbacks provided by the CLI layer (which can import ``output``).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from siftd.adapters.sdk import canonicalize_tool_name, extract_tool_hint
from siftd.domain.peek import PeekNarrativeBlock, PeekToolCall


@dataclass
class FollowEvent:
    """Parsed data from a single JSONL record (user or assistant turn).

    Module-local to decouple parse from render.  Not a domain type.
    """

    timestamp: str | None = None
    text: str | None = None
    tool_calls: list[tuple[str, int, list[str]]] = field(default_factory=list)
    """List of (canonical_name, count, hints)."""
    narrative: list[PeekNarrativeBlock] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    is_user: bool = False


def parse_record(
    record: dict,
    *,
    tool_aliases: dict[str, str] | None = None,
    hint_keys: dict[str, list[str]] | None = None,
    include_thinking: bool = False,
) -> FollowEvent | None:
    """Parse a single JSONL record into a FollowEvent.

    Returns None for tool_result records and non-message records.
    """
    record_type = record.get("type")
    if record_type not in ("user", "assistant"):
        return None

    msg = record.get("message") or {}
    content = msg.get("content")
    if content is None:
        content = []
    elif isinstance(content, str):
        content = [{"type": "text", "text": content}]
    elif not isinstance(content, list):
        content = []

    timestamp = record.get("timestamp")

    if record_type == "user":
        # Skip tool_result records
        if any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return None

        # Extract user text
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t:
                    text_parts.append(t)

        return FollowEvent(
            timestamp=timestamp,
            text="\n".join(text_parts) if text_parts else None,
            is_user=True,
        )

    # Assistant record
    usage = msg.get("usage") or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # Extract text (without [tool: X] placeholders — redundant with hint lines)
    text_parts = []
    narrative: list[PeekNarrativeBlock] = []
    pending_tools: list[PeekToolCall] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            narrative.append(PeekNarrativeBlock(block_type="text", content=block))
        elif isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(t)
                narrative.append(PeekNarrativeBlock(block_type="text", content=t))
        elif (
            include_thinking
            and isinstance(block, dict)
            and block.get("type") == "thinking"
        ):
            t = block.get("thinking") or block.get("text", "")
            if isinstance(t, str) and t.strip():
                text_parts.append(f"[thinking] {t}")
                narrative.append(PeekNarrativeBlock(block_type="thinking", content=t))
        elif isinstance(block, dict) and block.get("type") == "tool_use":
            raw_name = block.get("name", "unknown")
            canonical = raw_name
            if tool_aliases:
                canonical = canonicalize_tool_name(raw_name, tool_aliases)
            hint = None
            if hint_keys:
                raw_input = block.get("input")
                input_dict = raw_input if isinstance(raw_input, dict) else {}
                hint = extract_tool_hint(canonical, input_dict, hint_keys)
            pending_tools.append(PeekToolCall(tool_name=canonical, input=hint))

    # Derive tool_counter and tool_hints from pending_tools (already has
    # canonical names and hint strings from the first pass above).
    tool_counter: Counter[str] = Counter()
    tool_hints: dict[str, list[str]] = {}
    for tc in pending_tools:
        tool_counter[tc.tool_name] += 1
        if tc.input:
            tool_hints.setdefault(tc.tool_name, []).append(tc.input)

    tool_calls = [
        (name, count, tool_hints.get(name, []))
        for name, count in tool_counter.most_common()
    ]
    if pending_tools:
        narrative.append(PeekNarrativeBlock(block_type="tool_calls", tool_calls=pending_tools))

    text = "\n".join(text_parts) if text_parts else None
    if not text and not narrative and not tool_calls:
        return None

    return FollowEvent(
        timestamp=timestamp,
        text=text,
        tool_calls=tool_calls,
        narrative=narrative,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        is_user=False,
    )


def render_tool_line(name: str, count: int, hints: list[str]) -> str:
    """Render a single tool call line with hints.

    Rules:
    - Single call: → file.read: src/config.py
    - Multiple same tool: → file.read ×5: config.py, settings.py, main.py ... +2 more
    - No hint: → task.spawn
    - Max 3 hints shown, then elide
    """
    max_shown = 3

    if count == 1:
        if hints:
            return f"  \u2192 {name}: {hints[0]}"
        return f"  \u2192 {name}"

    # Multiple calls
    prefix = f"  \u2192 {name} \u00d7{count}"
    if not hints:
        return prefix

    shown = hints[:max_shown]
    remainder = len(hints) - max_shown
    hint_str = ", ".join(shown)
    if remainder > 0:
        hint_str += f" ... +{remainder} more"
    return f"{prefix}: {hint_str}"


def event_to_json(event: FollowEvent) -> dict:
    """Convert a FollowEvent to a JSON-serializable dict."""
    d: dict = {
        "role": "user" if event.is_user else "assistant",
        "timestamp": event.timestamp,
        "text": event.text,
    }
    if not event.is_user:
        d["input_tokens"] = event.input_tokens
        d["output_tokens"] = event.output_tokens
        d["tool_calls"] = [
            {"name": name, "count": count, "hints": hints}
            for name, count, hints in event.tool_calls
        ]
        if event.narrative:
            d["narrative"] = [
                {
                    "block_type": b.block_type,
                    **({"content": b.content} if b.content else {}),
                    **({"tool_calls": [
                        {"tool_name": tc.tool_name, "count": tc.count,
                         **({"input": tc.input} if tc.input else {})}
                        for tc in b.tool_calls
                    ]} if b.tool_calls else {}),
                }
                for b in event.narrative
            ]
    return d


def follow_session(
    path: Path,
    *,
    json_mode: bool = False,
    poll_interval: float = 0.5,
    render: Callable[[FollowEvent], None] | None = None,
    on_turn: Callable[[FollowEvent], None] | None = None,
    include_thinking: bool = False,
    timeout: float | None = None,
    ready_event: threading.Event | None = None,
) -> None:
    """Follow a live session file, emitting events as they arrive.

    Args:
        path: Path to the session JSONL file.
        json_mode: If True, output NDJSON to stdout.
        poll_interval: Seconds between polls for new data.
        render: Callback to render an event (text mode).  If None, events
                are silently consumed (useful with on_turn for testing).
        on_turn: Optional callback for testing (receives each FollowEvent).
        include_thinking: If True, include thinking blocks in output.
        timeout: If set, exit after this many seconds of wall-clock time.
    """
    # Resolve adapter for tool_aliases and hint_keys
    tool_aliases, hint_keys = _resolve_adapter_config(path)

    f = None
    start_time = time.time() if timeout is not None else None
    try:
        try:
            f = path.open("r", encoding="utf-8")
            stat = path.stat()
            last_inode = stat.st_ino
            last_dev = stat.st_dev
            f.seek(0, 2)  # Seek to end
            last_size = stat.st_size
            buf = ""
        except OSError:
            return
        finally:
            if ready_event is not None:
                ready_event.set()

        while True:
            if timeout is not None and start_time is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    break

            try:
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                break

            try:
                stat = path.stat()
                current_size = stat.st_size
            except OSError:
                break

            if stat.st_ino != last_inode or stat.st_dev != last_dev:
                if f:
                    f.close()
                try:
                    f = path.open("r", encoding="utf-8")
                except OSError:
                    break
                last_inode = stat.st_ino
                last_dev = stat.st_dev
                last_size = 0
                buf = ""
                f.seek(0)

            # Handle file truncation
            if current_size < last_size:
                f.seek(0)
                last_size = 0
                buf = ""

            if current_size == last_size:
                continue

            last_size = current_size
            chunk = f.read()
            if not chunk:
                continue

            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                event = parse_record(
                    record,
                    tool_aliases=tool_aliases,
                    hint_keys=hint_keys,
                    include_thinking=include_thinking,
                )
                if event is None:
                    continue

                if on_turn:
                    on_turn(event)

                if json_mode:
                    print(json.dumps(event_to_json(event), separators=(",", ":")))
                    sys.stdout.flush()
                elif render:
                    render(event)

    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()

    if not json_mode:
        print("\n(follow stopped)", file=sys.stderr)


def _resolve_adapter_config(
    path: Path,
) -> tuple[dict[str, str] | None, dict[str, list[str]] | None]:
    """Resolve tool_aliases and hint_keys from the adapter for this file."""
    from siftd.peek.reader import _find_adapter_for_file

    adapter = _find_adapter_for_file(path)
    if adapter is None:
        return None, None

    tool_aliases = getattr(adapter, "TOOL_ALIASES", None)
    hint_keys = getattr(adapter, "TOOL_HINT_KEYS", None)
    return tool_aliases, hint_keys
