"""Embedding backends for semantic search.

Fallback chain:
1. Ollama (if running locally)
2. fastembed (local ONNX, no network)
3. API (configurable, requires key)

Usage:
    from embeddings import get_backend
    backend = get_backend()
    vectors = backend.embed(["hello", "world"])
"""

from embeddings.base import EmbeddingBackend, get_backend

__all__ = ["EmbeddingBackend", "get_backend"]
