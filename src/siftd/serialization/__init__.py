"""Serialization layer — domain objects to structured data.

Transforms normalized siftd data (conversations, turns, narrative blocks)
into JSON-safe dicts. Used by serve routes (API responses), output/json_fmt
(CLI --json), and any future structured-data consumer.

This layer sits alongside domain/utilities — importable by all higher layers.
"""

from siftd.serialization.conversations import (
    serialize_conversation_detail,
    serialize_conversation_list,
    serialize_conversation_summary,
)
from siftd.serialization.health import serialize_health_status
from siftd.serialization.narrative import (
    JsonEmitter,
    NarrativeEmitter,
    walk_narrative,
)

__all__ = [
    "JsonEmitter",
    "NarrativeEmitter",
    "serialize_conversation_detail",
    "serialize_conversation_list",
    "serialize_conversation_summary",
    "serialize_health_status",
    "walk_narrative",
]
