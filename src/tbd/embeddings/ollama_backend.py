"""Ollama embedding backend.

Connects to a locally running Ollama instance and uses whatever
embedding model is available.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# Models to try in order of preference (smaller/faster first)
_PREFERRED_MODELS = [
    "nomic-embed-text",
    "mxbai-embed-large",
    "all-minilm",
    "snowflake-arctic-embed",
]

_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaBackend:
    """Embedding backend using a local Ollama instance."""

    name = "ollama"

    def __init__(self, base_url: str = _DEFAULT_BASE_URL, model: str | None = None):
        self.base_url = base_url
        self.model = model or self._find_model()
        self.dimension = self._probe_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts."""
        return [self.embed_one(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text via Ollama API."""
        payload = json.dumps({"model": self.model, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data["embedding"]
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Ollama embed failed: {e}") from e

    def _find_model(self) -> str:
        """Find an available embedding model on the Ollama instance."""
        models = self._list_models()
        # Try preferred models first
        for preferred in _PREFERRED_MODELS:
            for m in models:
                if preferred in m:
                    return m
        # Fall back to any model with 'embed' in the name
        for m in models:
            if "embed" in m:
                return m
        raise RuntimeError(
            f"No embedding model found in Ollama. Available: {models}\n"
            f"Pull one with: ollama pull nomic-embed-text"
        )

    def _list_models(self) -> list[str]:
        """List models available on the Ollama instance."""
        req = urllib.request.Request(f"{self.base_url}/api/tags")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return [m["name"] for m in data.get("models", [])]
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            raise ConnectionError(f"Cannot connect to Ollama at {self.base_url}: {e}") from e

    def _probe_dimension(self) -> int:
        """Determine embedding dimension by embedding a test string."""
        vec = self.embed_one("test")
        return len(vec)
