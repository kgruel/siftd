"""Embedding backends for semantic search.

Fallback chain:
1. Ollama (if running locally)
2. fastembed (local ONNX, no network)
3. API (configurable, requires key)

Usage:
    from siftd.embeddings import embeddings_available, get_backend

    if embeddings_available():
        backend = get_backend()
        vectors = backend.embed(["hello", "world"])

Note: Embedding functionality requires the [embed] extra.
Install via ``siftd install embed`` or directly with the appropriate
package manager command.
"""

from .availability import (
    EmbeddingsNotAvailable,
    embeddings_available,
    require_embeddings,
)

# Always export availability functions
__all__ = [
    "embeddings_available",
    "require_embeddings",
    "EmbeddingsNotAvailable",
]

# Conditionally export embedding functionality when deps are available
if embeddings_available():
    from .base import EmbeddingBackend, get_backend, invalidate_backend_cache
    from .indexer import SCHEMA_VERSION, IndexStats, build_embeddings_index

    __all__ += ["EmbeddingBackend", "get_backend", "invalidate_backend_cache", "IndexStats", "build_embeddings_index", "SCHEMA_VERSION"]
