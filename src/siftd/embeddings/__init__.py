"""Embedding backends for semantic search.

Resolution is config-driven (``[embed].backend``): a remote OpenAI-compatible provider
(base install, just an API key) or the local ``fastembed`` ONNX backend (the [embed]
extra). See ``base.py`` for the deterministic resolver and ``availability.py`` for
``embedding_status()``.

Usage:
    from siftd.embeddings import embedding_status
    from siftd.embeddings.base import get_backend

    if embedding_status().usable:
        backend = get_backend()
        vectors = backend.embed_documents(["hello", "world"])

Only the availability/status layer is re-exported here — it is light and always
importable. Backend and index symbols are imported from their concrete submodules
(``siftd.embeddings.base``, ``siftd.embeddings.indexer``) so the heavier vector-math
imports stay off the light CLI paths (see tests/architecture/test_hard_rules.py).
"""

from .availability import (
    EmbeddingsNotAvailable,
    EmbedStatus,
    embedding_status,
    embeddings_available,
    require_embeddings,
)

__all__ = [
    "EmbedStatus",
    "EmbeddingsNotAvailable",
    "embedding_status",
    "embeddings_available",
    "require_embeddings",
]
