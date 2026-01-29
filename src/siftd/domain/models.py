"""Domain models for siftd.

Nested structure: Conversation → Prompt → Response → ToolCall
These are pure data objects, decoupled from storage.
"""

from dataclasses import dataclass, field


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


@dataclass
class ToolCall:
    """A tool invocation within a response."""
    tool_name: str
    input: dict
    result: dict | None = None
    status: str = "pending"
    external_id: str | None = None
    timestamp: str | None = None


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


@dataclass
class Prompt:
    """A user prompt within a conversation."""
    timestamp: str
    content: list[ContentBlock] = field(default_factory=list)
    responses: list[Response] = field(default_factory=list)
    external_id: str | None = None


@dataclass
class Harness:
    """Metadata about the CLI harness that produced the conversation."""
    name: str
    source: str | None = None
    log_format: str | None = None
    display_name: str | None = None


@dataclass
class Conversation:
    """A complete conversation from a harness.

    Top-level domain object representing an interaction session.
    """
    external_id: str
    harness: Harness
    started_at: str
    prompts: list[Prompt] = field(default_factory=list)
    workspace_path: str | None = None
    ended_at: str | None = None
