"""Deterministic, config-driven backend resolution (base lane — no fastembed).

Resolution reads [embed] config: an explicit backend is constructed exactly (config errors
raise, never fall through); unset falls back to fastembed-if-installed-else-none.
"""

import types

import pytest

from siftd.embeddings import base


def _cfg(monkeypatch, **values):
    """Point base's config reads at an in-memory dict and clear the resolver cache."""
    import siftd.config as config

    monkeypatch.setattr(config, "get_config", lambda key: values.get(key))
    base.invalidate_backend_cache()


def _fake_fastembed(monkeypatch, present=True):
    fake = types.SimpleNamespace(name="fastembed", model="bge", dimension=384)
    monkeypatch.setattr(base, "_try_fastembed", lambda: fake if present else None)
    return fake


def test_off_resolves_to_none(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "off"})
    assert base.resolve_backend() is None


def test_unset_uses_fastembed_when_present(monkeypatch):
    _cfg(monkeypatch)
    fake = _fake_fastembed(monkeypatch, present=True)
    assert base.resolve_backend() is fake


def test_unset_is_none_without_fastembed(monkeypatch):
    _cfg(monkeypatch)
    _fake_fastembed(monkeypatch, present=False)
    assert base.resolve_backend() is None


def test_explicit_fastembed_missing_raises(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "fastembed"})
    _fake_fastembed(monkeypatch, present=False)
    with pytest.raises(base.EmbeddingConfigError, match="install embed"):
        base.resolve_backend()


def test_remote_preset_builds_from_defaults(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "sk-literal"})
    b = base.resolve_backend()
    assert b.name == "remote:voyage"
    assert b.model == "voyage-4-lite"
    assert b.dimension == 1024


def test_config_dimensions_override_preset(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "k", "embed.dimensions": "512"})
    b = base.resolve_backend()
    assert b.dimension == 512
    assert b._dimensions_param == 512


def test_preset_dimensions_param_name_resolved(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "k"})
    assert base.resolve_backend()._dimensions_param_name == "output_dimension"
    _cfg(monkeypatch, **{"embed.backend": "openai", "embed.api_key": "k"})
    assert base.resolve_backend()._dimensions_param_name == "dimensions"


def test_malformed_dimensions_raises(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "k", "embed.dimensions": "abc"})
    with pytest.raises(base.EmbeddingConfigError, match="positive integer"):
        base.resolve_backend()


def test_nonpositive_dimensions_raises(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "k", "embed.dimensions": "0"})
    with pytest.raises(base.EmbeddingConfigError, match="positive integer"):
        base.resolve_backend()


def test_unknown_backend_raises_config_error(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "nope"})
    with pytest.raises(base.EmbeddingConfigError, match="not a known backend"):
        base.resolve_backend()


def test_ollama_requires_model(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "ollama"})
    with pytest.raises(base.EmbeddingConfigError, match=r"requires embed\.model"):
        base.resolve_backend()


def test_custom_requires_base_url(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "custom", "embed.model": "m"})
    with pytest.raises(base.EmbeddingConfigError, match=r"requires embed\.base_url"):
        base.resolve_backend()


def test_unresolvable_key_ref_raises(monkeypatch):
    monkeypatch.delenv("SIFTD_NOPE_KEY", raising=False)
    _cfg(monkeypatch, **{"embed.backend": "openai", "embed.api_key": "env:SIFTD_NOPE_KEY"})
    with pytest.raises(base.EmbeddingConfigError, match="unresolvable"):
        base.resolve_backend()


def test_resolution_is_cached_until_invalidated(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "voyage", "embed.api_key": "k"})
    b1 = base.resolve_backend()
    b2 = base.resolve_backend()
    assert b1 is b2
    base.invalidate_backend_cache()
    assert base.resolve_backend() is not b1


def test_cache_keys_on_config(monkeypatch):
    """A config change resolves a different backend without an explicit invalidate."""
    import siftd.config as config

    values = {"embed.backend": "voyage", "embed.api_key": "k"}
    monkeypatch.setattr(config, "get_config", lambda key: values.get(key))
    base.invalidate_backend_cache()
    voyage = base.resolve_backend()
    values["embed.backend"] = "openai"
    openai = base.resolve_backend()
    assert voyage.name == "remote:voyage" and openai.name == "remote:openai"


def test_get_backend_is_config_driven(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "openai", "embed.api_key": "k"})
    # Resolution reads config exactly — there is no override argument.
    b = base.get_backend()
    assert b.name == "remote:openai"


def test_get_backend_unknown_config_raises(monkeypatch):
    _cfg(monkeypatch, **{"embed.backend": "nonexistent_backend"})
    with pytest.raises(base.EmbeddingConfigError, match="not a known backend"):
        base.get_backend()


def test_get_backend_none_raises_not_available(monkeypatch):
    from siftd.embeddings.availability import EmbeddingsNotAvailable

    _cfg(monkeypatch, **{"embed.backend": "off"})
    with pytest.raises(EmbeddingsNotAvailable):
        base.get_backend()
