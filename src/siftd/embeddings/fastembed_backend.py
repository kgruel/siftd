"""FastEmbed backend — local ONNX embeddings, no network required.

Requires: pip install fastembed
"""

from __future__ import annotations

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class FastEmbedBackend:
    """Embedding backend using fastembed (local ONNX inference)."""

    name = "fastembed"

    def __init__(self, model: str = _DEFAULT_MODEL):
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise ImportError(
                "fastembed not installed. Install with: pip install fastembed"
            )

        self.model = model
        self._embedder = TextEmbedding(model_name=model)
        self.dimension = self._probe_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        # fastembed returns a generator of numpy arrays
        embeddings = list(self._embedder.embed(texts))
        return [e.tolist() for e in embeddings]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text."""
        return self.embed([text])[0]

    def _probe_dimension(self) -> int:
        """Determine embedding dimension from model."""
        vec = self.embed_one("test")
        return len(vec)
