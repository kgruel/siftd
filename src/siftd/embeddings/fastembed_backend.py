"""FastEmbed backend — local ONNX embeddings, no network required.

Requires the [embed] extra: ``siftd install embed``. Keeps ``bge-small-en-v1.5``, which is
prefix-free/symmetric by design (the safest local default): query and document embeddings
are identical, so intent needs no per-call handling.
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
                "fastembed not installed. Install with: siftd install embed"
            )

        self.model = model
        self._embedder = TextEmbedding(model_name=model)
        self.dimension = self._resolve_dimension()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents. fastembed returns a generator of numpy arrays."""
        return [e.tolist() for e in self._embedder.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query. bge is symmetric, so this matches document embedding."""
        return self.embed_documents([text])[0]

    def _resolve_dimension(self) -> int:
        """Determine the dimension by a one-off local probe — in-process ONNX inference,
        no network. (Cross-version fastembed model-description introspection is too
        fragile to rely on; the local probe is cheap enough per the design.)"""
        return len(self.embed_documents(["dimension probe"])[0])
