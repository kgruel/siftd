"""Embedding backend protocol and fallback chain resolution."""

from __future__ import annotations

import sys
from typing import Protocol


class EmbeddingBackend(Protocol):
    """Protocol for embedding backends."""

    name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of embedding vectors."""
        ...

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Convenience wrapper."""
        ...


def get_backend(preferred: str | None = None, verbose: bool = False) -> EmbeddingBackend:
    """Resolve an embedding backend using the fallback chain.

    Order: ollama → fastembed → api
    If preferred is set, try that backend first (fail if unavailable).
    """
    if preferred:
        backend = _try_backend(preferred, verbose)
        if backend is None:
            raise RuntimeError(f"Requested embedding backend '{preferred}' is not available")
        return backend

    # Fallback chain
    for name in ("ollama", "fastembed"):
        backend = _try_backend(name, verbose)
        if backend is not None:
            return backend

    raise RuntimeError(
        "No embedding backend available.\n"
        "Install one of:\n"
        "  - Ollama (running locally with an embedding model)\n"
        "  - fastembed: pip install fastembed\n"
    )


def _try_backend(name: str, verbose: bool) -> EmbeddingBackend | None:
    """Try to initialize a backend by name. Returns None if unavailable."""
    try:
        if name == "ollama":
            from siftd.embeddings.ollama_backend import OllamaBackend
            backend = OllamaBackend()
            if verbose:
                print(f"Using embedding backend: ollama ({backend.model})", file=sys.stderr)
            return backend
        elif name == "fastembed":
            from siftd.embeddings.fastembed_backend import FastEmbedBackend
            backend = FastEmbedBackend()
            if verbose:
                print(f"Using embedding backend: fastembed ({backend.model})", file=sys.stderr)
            return backend
        else:
            raise ValueError(f"Unknown backend: {name}")
    except (ImportError, ConnectionError, RuntimeError):
        return None
