"""Embedding backend protocol, exception taxonomy, and deterministic resolution.

Resolution is config-driven and deterministic — no probe-in-order chain. ``embed.backend``
names exactly one backend; a *configuration* failure (unknown preset, unresolvable key
ref, a preset that needs a model with none set) is an error, never a silent fallthrough.
Remote backends are only ever active by explicit config — configuring one is the
data-egress opt-in. Unset ⇒ the local ``fastembed`` backend if the [embed] extra is
installed, else no backend.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from siftd.embeddings.remote import RemoteBackend


class EmbeddingBackend(Protocol):
    """Protocol for embedding backends. Intent is chosen at the call site (indexer →
    documents, search → query); the backend maps it to the provider-native mechanism."""

    name: str  # "remote:voyage", "remote:ollama", "fastembed"
    model: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class EmbeddingError(Exception):
    """Base for embedding backend failures."""


class EmbeddingConfigError(EmbeddingError):
    """[embed] config is present but unusable — bad backend name, unresolvable key ref,
    or a preset missing a required model/base_url. Never retried, never degraded."""


class EmbeddingTransientError(RuntimeError, EmbeddingError):
    """A reachability blip — timeout, 429, 5xx, or network error. A single query can
    degrade to FTS on this class (wired in a later slice); selection is unchanged.

    Subclasses RuntimeError so the existing search retry tuples (api/search.py,
    siftd/search.py) and the CLI boundary guards (cli/search.py) catch it as-is — the CLI
    layer cannot import from embeddings, so the RuntimeError base is what keeps a transient
    remote failure a status.error() instead of a traceback until slice 4 wires the full
    degrade-to-fts. Config errors deliberately do NOT subclass RuntimeError (never retried).
    """


# The [embed] keys that determine which backend gets built. Used both to resolve and as
# the process cache key, so a config change in a long-lived process rebuilds naturally.
class _EmbedSettings(NamedTuple):
    backend: str  # normalized; "" when unset
    api_key: str
    model: str
    dimensions: int | None
    base_url: str
    query_prefix: str
    document_prefix: str


def _read_embed_config() -> _EmbedSettings:
    from siftd.config import get_config

    def s(key: str) -> str:
        return (get_config(f"embed.{key}") or "").strip()

    dims_raw = get_config("embed.dimensions")
    dimensions: int | None = None
    if dims_raw is not None and str(dims_raw).strip():
        # A malformed dimensions value is a config failure, raised like its siblings —
        # never silently coerced to the preset default (that would hide the mistake).
        try:
            dimensions = int(dims_raw)
        except ValueError:
            raise EmbeddingConfigError(
                f"embed.dimensions must be a positive integer, got {dims_raw!r}"
            ) from None
        if dimensions <= 0:
            raise EmbeddingConfigError(
                f"embed.dimensions must be a positive integer, got {dims_raw!r}"
            )
    return _EmbedSettings(
        backend=s("backend").lower(),
        api_key=get_config("embed.api_key") or "",
        model=s("model"),
        dimensions=dimensions,
        base_url=s("base_url"),
        query_prefix=get_config("embed.query_prefix") or "",
        document_prefix=get_config("embed.document_prefix") or "",
    )


# Process-level cache keyed on the resolved config, cleared by invalidate_backend_cache()
# after a runtime failure. A bare fastembed model load is expensive; remote construction
# is cheap but harmless to cache.
_backend_cache: dict[_EmbedSettings, EmbeddingBackend | None] = {}


def resolve_backend() -> EmbeddingBackend | None:
    """Resolve the configured embedding backend, or None when none is active.

    None ⇒ ``embed.backend = "off"``, or unset with the [embed] extra not installed.
    Raises EmbeddingConfigError when config is present but unusable.
    """
    settings = _read_embed_config()
    if settings in _backend_cache:
        return _backend_cache[settings]
    backend = _resolve_uncached(settings)
    _backend_cache[settings] = backend
    return backend


def _resolve_uncached(settings: _EmbedSettings) -> EmbeddingBackend | None:
    name = settings.backend
    if name == "off":
        return None
    if name in ("", "fastembed"):
        backend = _try_fastembed()
        if backend is None and name == "fastembed":
            raise EmbeddingConfigError(
                "embed.backend = \"fastembed\" but the [embed] extra is not installed; "
                "run `siftd install embed` or set embed.backend to a remote preset"
            )
        return backend
    return _build_remote(name, settings)


def _try_fastembed() -> EmbeddingBackend | None:
    try:
        from siftd.embeddings.fastembed_backend import FastEmbedBackend

        return FastEmbedBackend()
    except ImportError:
        return None


def _build_remote(name: str, settings: _EmbedSettings) -> RemoteBackend:
    from siftd.credentials import TokenRefError, resolve_token_ref
    from siftd.embeddings.presets import get_preset, preset_names
    from siftd.embeddings.remote import RemoteBackend

    preset = get_preset(name)
    if preset is None:
        valid = ", ".join([*preset_names(), "fastembed", "off"])
        raise EmbeddingConfigError(
            f"embed.backend = {name!r} is not a known backend (valid: {valid})"
        )

    base_url = settings.base_url or preset.base_url
    if not base_url:
        raise EmbeddingConfigError(
            f"embed.backend = {name!r} requires embed.base_url"
        )
    model = settings.model or preset.default_model
    if not model:
        raise EmbeddingConfigError(
            f"embed.backend = {name!r} requires embed.model (no preset default)"
        )

    api_key = ""
    if settings.api_key:
        try:
            api_key = resolve_token_ref(settings.api_key)
        except TokenRefError as e:
            raise EmbeddingConfigError(f"embed.api_key is unresolvable: {e}") from e

    # A preset's default dimension belongs to its default model; an overridden model's
    # dimension is learned from the first response unless embed.dimensions sets it explicitly.
    reported_dim = settings.dimensions if settings.dimensions is not None else (
        preset.default_dimensions if model == preset.default_model else None
    )
    return RemoteBackend(
        preset_name=name,
        base_url=base_url,
        model=model,
        intent_style=preset.intent_style,
        max_batch=preset.max_batch,
        api_key=api_key,
        dimension=reported_dim,
        dimensions_param=settings.dimensions,
        dimensions_param_name=preset.dimensions_param,
        query_prefix=settings.query_prefix,
        document_prefix=settings.document_prefix,
    )


def get_backend(verbose: bool = False) -> EmbeddingBackend:
    """Return the configured embedding backend, or raise if none can be constructed.

    Config-driven only (``resolve_backend`` over ``[embed]``). There is no per-call
    backend override — the backend is chosen by ``embed.backend`` and switching it is a
    config change (then ``siftd embed --rebuild``), never a flag on search or embed.
    """
    backend = resolve_backend()
    if backend is None:
        from siftd.embeddings.availability import require_embeddings

        require_embeddings("Semantic search")  # status-aware message; raises
        raise EmbeddingConfigError("no embedding backend is configured")
    if verbose:
        print(f"Using embedding backend: {backend.name} ({backend.model})", file=sys.stderr)
    return backend


def invalidate_backend_cache() -> None:
    """Clear the resolved-backend cache so the next resolution rebuilds.

    Called after a cached backend fails at runtime (e.g. a long-lived process whose remote
    endpoint became unreachable).
    """
    _backend_cache.clear()
