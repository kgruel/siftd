"""Embedding preset reference data — loading, per-provider fields, validation."""

import pytest

from siftd.embeddings import presets


def test_shipped_presets_carry_expected_fields():
    loaded = presets.load_presets()
    voyage = loaded["voyage"]
    assert voyage.base_url == "https://api.voyageai.com/v1"
    assert voyage.default_model == "voyage-4-lite"
    assert voyage.default_dimensions == 1024
    assert voyage.intent_style == "param:input_type"
    assert voyage.max_batch == 128


def test_truncation_param_name_per_provider():
    loaded = presets.load_presets()
    # Voyage and Mistral spell the matryoshka-truncation field `output_dimension`.
    assert loaded["voyage"].dimensions_param == "output_dimension"
    assert loaded["mistral"].dimensions_param == "output_dimension"
    # Everyone else defaults to OpenAI's `dimensions`.
    assert loaded["openai"].dimensions_param == "dimensions"
    assert loaded["jina"].dimensions_param == "dimensions"
    assert loaded["ollama"].dimensions_param == "dimensions"


def test_ollama_and_custom_have_no_required_defaults():
    loaded = presets.load_presets()
    assert loaded["ollama"].default_model is None
    assert loaded["custom"].base_url == ""
    assert loaded["custom"].default_dimensions is None


def test_unknown_intent_style_rejected():
    bad = """
    [[preset]]
    name = "broken"
    base_url = "https://x/v1"
    intent_style = "magic"
    """
    with pytest.raises(ValueError, match="unknown intent_style 'magic'"):
        presets._parse_toml(bad)


def test_dimensions_param_defaults_when_omitted():
    row = """
    [[preset]]
    name = "plain"
    base_url = "https://x/v1"
    intent_style = "none"
    """
    parsed = presets._parse_toml(row)
    assert parsed["plain"].dimensions_param == "dimensions"
