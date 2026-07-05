"""Generic OpenAI-compatible remote embedding backend.

Every surveyed provider (Voyage, OpenAI, Jina, Mistral, Gemini's compat layer, Cohere's
compat endpoint) and every local server (Ollama, llama.cpp, vLLM, LM Studio) speaks
``POST {base_url}/embeddings`` with an ``input`` array and Bearer auth — so one client on
``httpx`` (already a base dependency) covers the whole matrix. Provider differences are
data (``embed_presets.toml``); the only code branch is ``intent_style``.

Constructed only by ``siftd.embeddings.base`` from resolved config — never active unless
``[embed].backend`` names a remote preset (configuring it is the data-egress opt-in).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from siftd.embeddings.base import EmbeddingConfigError, EmbeddingTransientError

if TYPE_CHECKING:
    import httpx

# Bounded retry for transient failures (timeout / 429 / 5xx / network). Config errors
# (401/403, malformed request) are never retried.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_DEFAULT_TIMEOUT_S = 30.0


class RemoteBackend:
    """OpenAI-compatible embeddings client for one configured preset.

    ``dimension`` may be None until the first embedding call learns it from the response
    (ollama/custom without a preset default and no ``embed.dimensions`` override).
    """

    def __init__(
        self,
        *,
        preset_name: str,
        base_url: str,
        model: str,
        intent_style: str,
        max_batch: int,
        api_key: str = "",
        dimension: int | None = None,
        dimensions_param: int | None = None,
        dimensions_param_name: str = "dimensions",
        query_prefix: str = "",
        document_prefix: str = "",
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_attempts: int = _MAX_ATTEMPTS,
        backoff_base: float = _BACKOFF_BASE_S,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.name = f"remote:{preset_name}"
        self.model = model
        # int once known; None only until the first response is learned (ollama/custom
        # with no preset default). Typed int for EmbeddingBackend protocol conformance.
        self.dimension: int = dimension  # type: ignore[assignment]
        self._url = base_url.rstrip("/") + "/embeddings"
        self._intent_style = intent_style
        self._max_batch = max(1, max_batch)
        self._api_key = api_key
        # Sent in the request body only when the user configured embed.dimensions
        # (provider matryoshka truncation); absent ⇒ the model's native dimension. The
        # wire field name varies by provider (OpenAI `dimensions`, Voyage/Mistral
        # `output_dimension`) — carried per-preset in embed_presets.toml.
        self._dimensions_param = dimensions_param
        self._dimensions_param_name = dimensions_param_name
        self._query_prefix = query_prefix
        self._document_prefix = document_prefix
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._client = client
        self._sleep = sleep

    # --- Protocol surface -------------------------------------------------- #

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, intent="document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], intent="query")[0]

    # --- Internals --------------------------------------------------------- #

    def _embed(self, texts: list[str], *, intent: str) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._max_batch):
            batch = texts[start : start + self._max_batch]
            out.extend(self._embed_batch(batch, intent=intent))
        return out

    def _embed_batch(self, batch: list[str], *, intent: str) -> list[list[float]]:
        body = self._build_body(batch, intent=intent)
        data = self._post_with_retry(body)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise EmbeddingTransientError(
                f"{self.name}: embeddings response missing 'data' array"
            )
        # OpenAI-compat responses carry an `index` per row; order by it defensively.
        ordered = sorted(rows, key=lambda r: r.get("index", 0))
        vectors = [list(r["embedding"]) for r in ordered]
        # A response with the wrong row count is a malformed/buggy server, not an accepted
        # result — transient (retried-or-degraded), never silently truncated (a short batch
        # would drop a chunk yet still stamp the conversation's fingerprint as current).
        if len(vectors) != len(batch):
            raise EmbeddingTransientError(
                f"{self.name}: expected {len(batch)} embeddings, got {len(vectors)}"
            )
        if self.dimension:
            # Known dimension ⇒ validate. A provider that silently ignores the truncation
            # param would otherwise store ragged vectors the index_meta can't detect.
            for v in vectors:
                if len(v) != self.dimension:
                    raise EmbeddingConfigError(
                        f"{self.name}: expected dimension {self.dimension} but {self.model} "
                        f"returned {len(v)}; the truncation parameter may be unsupported "
                        f"for this model (check embed.dimensions)"
                    )
        elif vectors:  # unset (None) until the first response — learn it
            self.dimension = len(vectors[0])
        return vectors

    def _build_body(self, batch: list[str], *, intent: str) -> dict:
        inputs = batch
        # No `encoding_format`: every provider defaults to float arrays when omitted, and
        # some (Voyage) reject the OpenAI `encoding_format:"float"` value outright.
        body: dict = {"model": self.model}
        if self._dimensions_param is not None:
            body[self._dimensions_param_name] = self._dimensions_param
        if self._intent_style == "param:input_type":
            body["input_type"] = "document" if intent == "document" else "query"
        elif self._intent_style == "param:task":
            body["task"] = "retrieval.passage" if intent == "document" else "retrieval.query"
        elif self._intent_style == "prefix":
            prefix = self._document_prefix if intent == "document" else self._query_prefix
            if prefix:
                inputs = [prefix + t for t in batch]
        body["input"] = inputs
        return body

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _post_with_retry(self, body: dict) -> dict:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        client = self._ensure_client()

        delay = self._backoff_base
        last_transient: EmbeddingTransientError | None = None
        for attempt in range(self._max_attempts):
            is_last = attempt == self._max_attempts - 1
            try:
                resp = client.post(self._url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_transient = EmbeddingTransientError(f"{self.name}: request failed: {e}")
                if is_last:
                    break
                self._sleep(delay)
                delay *= 2
                continue

            status = resp.status_code
            if status == 200:
                return resp.json()
            if status in (401, 403):
                raise EmbeddingConfigError(
                    f"{self.name}: authentication failed (HTTP {status}); "
                    f"check embed.api_key"
                )
            if status == 429 or 500 <= status < 600:
                last_transient = EmbeddingTransientError(
                    f"{self.name}: transient error (HTTP {status})"
                )
                if is_last:
                    break
                wait = _retry_after(resp) if status == 429 else None
                self._sleep(wait if wait is not None else delay)
                delay *= 2
                continue
            # Other 4xx: bad request (e.g. unknown model, malformed input) — not retryable.
            raise EmbeddingConfigError(
                f"{self.name}: request rejected (HTTP {status}): {resp.text[:200]}"
            )

        raise last_transient or EmbeddingTransientError(f"{self.name}: embedding request failed")


def _retry_after(resp: httpx.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds only) into a float, or None."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
