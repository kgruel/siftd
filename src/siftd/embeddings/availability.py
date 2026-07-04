"""Embedding availability — is a backend *configured/installed*?

Answers configuration/installation, NOT reachability: a remote backend that is configured
with a resolvable key counts as available even before any request is made; a runtime blip
degrades a single query elsewhere. ``embedding_status()`` is the source of truth;
``embeddings_available()`` is the boolean shim its call sites already use.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmbedStatus:
    """Resolved embedding availability. ``backend`` is the resolved name when usable
    (e.g. "remote:voyage", "fastembed"), else None; ``reason`` is human-readable."""

    backend: str | None
    usable: bool
    reason: str


def _fastembed_importable() -> bool:
    try:
        import fastembed  # noqa: F401

        return True
    except ImportError:
        return False


def embedding_status() -> EmbedStatus:
    """Report whether an embedding backend is configured/installed and usable.

    Cheap by construction: the fastembed case checks import only (no ONNX model load) and
    the remote case validates config without any network call.
    """
    from siftd.embeddings.base import EmbeddingConfigError, _read_embed_config

    # Config-shape failures (e.g. malformed embed.dimensions) surface as unusable-with-
    # reason, not an exception — status/doctor must never crash on a bad config value.
    try:
        settings = _read_embed_config()
    except EmbeddingConfigError as e:
        return EmbedStatus(None, False, str(e))
    name = settings.backend

    if name == "off":
        return EmbedStatus(None, False, 'embeddings disabled (embed.backend = "off")')

    if name in ("", "fastembed"):
        if _fastembed_importable():
            return EmbedStatus("fastembed", True, "local fastembed backend installed")
        if name == "fastembed":
            return EmbedStatus(
                None,
                False,
                'embed.backend = "fastembed" but the [embed] extra is not installed; '
                "run `siftd install embed`",
            )
        return EmbedStatus(
            None,
            False,
            "no embedding backend configured; set embed.backend or install siftd[embed]",
        )

    # Remote preset / custom — validate config (no network) by attempting construction.
    from siftd.embeddings.base import _build_remote

    try:
        backend = _build_remote(name, settings)
    except EmbeddingConfigError as e:
        return EmbedStatus(None, False, str(e))
    return EmbedStatus(backend.name, True, f"remote backend ({backend.model})")


def embeddings_available() -> bool:
    """Whether a usable embedding backend is configured/installed.

    NEW semantics (was "fastembed importable"): a configured remote backend counts as
    available even without the [embed] extra. Thin shim over ``embedding_status().usable``.
    """
    return embedding_status().usable


class EmbeddingsNotAvailable(Exception):
    """Raised when embedding functionality is requested but no backend is available."""

    def __init__(self, operation: str = "This operation", reason: str | None = None):
        self.operation = operation
        self.reason = reason or embedding_status().reason
        self.message = (
            f"{operation} requires an embedding backend.\n\n"
            f"  {self.reason}\n\n"
            "Configure a remote backend (embed.backend = voyage|openai|...) or install\n"
            "the local extra:\n"
            "  siftd install embed\n\n"
            "Or use FTS5 search instead:\n"
            '  siftd query -s "your search"'
        )
        super().__init__(self.message)


def require_embeddings(operation: str = "This operation") -> None:
    """Raise EmbeddingsNotAvailable unless a usable backend is configured/installed."""
    status = embedding_status()
    if not status.usable:
        raise EmbeddingsNotAvailable(operation, reason=status.reason)
