"""Tests for model name parsing."""

import pytest

from siftd.model_names import parse_model_name


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Claude 4.x: claude-{variant}-{major}-{minor}-{YYYYMMDD}
        ("claude-opus-4-5-20251101", {"name": "claude-opus-4-5", "creator": "anthropic", "family": "claude", "version": "4.5", "variant": "opus", "released": "2025-11-01"}),
        ("claude-haiku-4-5-20251001", {"name": "claude-haiku-4-5", "creator": "anthropic", "family": "claude", "version": "4.5", "variant": "haiku", "released": "2025-10-01"}),
        ("claude-sonnet-4-5-20250929", {"name": "claude-sonnet-4-5", "creator": "anthropic", "family": "claude", "version": "4.5", "variant": "sonnet", "released": "2025-09-29"}),
        # Claude 3.x: claude-{major}-{minor}-{variant}-{YYYYMMDD}
        ("claude-3-5-haiku-20241022", {"name": "claude-3-5-haiku", "creator": "anthropic", "family": "claude", "version": "3.5", "variant": "haiku", "released": "2024-10-22"}),
        # Claude 3: claude-{major}-{variant}-{YYYYMMDD}
        ("claude-3-haiku-20240307", {"name": "claude-3-haiku", "creator": "anthropic", "family": "claude", "version": "3", "variant": "haiku", "released": "2024-03-07"}),
    ],
    ids=["opus-4.5", "haiku-4.5", "sonnet-4.5", "haiku-3.5", "haiku-3"],
)
def test_claude_models(raw, expected):
    assert parse_model_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gemini-3-pro-preview", {"name": "gemini-3-pro", "creator": "google", "family": "gemini", "version": "3", "variant": "pro", "released": None}),
        ("gemini-3-flash-preview", {"name": "gemini-3-flash", "creator": "google", "family": "gemini", "version": "3", "variant": "flash", "released": None}),
        ("gemini-2.5-pro", {"name": "gemini-2.5-pro", "creator": "google", "family": "gemini", "version": "2.5", "variant": "pro", "released": None}),
    ],
    ids=["3-pro-preview", "3-flash-preview", "2.5-pro"],
)
def test_gemini_models(raw, expected):
    assert parse_model_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<synthetic>", {"name": "<synthetic>", "creator": None, "family": None, "version": None, "variant": None, "released": None}),
        ("some-unknown-model-v2", {"name": "some-unknown-model-v2", "creator": None, "family": None, "version": None, "variant": None, "released": None}),
    ],
    ids=["synthetic", "unknown"],
)
def test_fallback_models(raw, expected):
    assert parse_model_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        # Dot and dash spellings of the dateless 4.x form collapse to one canonical
        # (dash) name, so a single pricing-reference entry prices both.
        ("claude-haiku-4.5", "claude-haiku-4-5"),
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-sonnet-4.6", "claude-sonnet-4-6"),
        ("claude-opus-4-7", "claude-opus-4-7"),
    ],
    ids=["haiku-dot", "haiku-dash", "sonnet-dot", "opus-dash"],
)
def test_dateless_dot_dash_canonicalizes(raw, expected_name):
    parsed = parse_model_name(raw)
    assert parsed["name"] == expected_name
    assert parsed["creator"] == "anthropic"
    assert parsed["family"] == "claude"


# ---------------------------------------------------------------------------
# creator: the maker, which prices a response its serving provider has no price for
# ---------------------------------------------------------------------------

# Shapes taken from the upstream id lists (models.dev: anthropic, openai, google),
# not from any one machine's database.
@pytest.mark.parametrize(
    "raw,name,creator,family",
    [
        # claude: no minor and no date, and the fable variant
        ("claude-opus-5", "claude-opus-5", "anthropic", "claude"),
        ("claude-sonnet-5", "claude-sonnet-5", "anthropic", "claude"),
        ("claude-fable-5", "claude-fable-5", "anthropic", "claude"),
        ("claude-fable-5-1", "claude-fable-5-1", "anthropic", "claude"),
        # openai: gpt-*, the o-series, codex-*. The id is the name: a dated snapshot
        # and its alias are separate priced models upstream.
        ("gpt-4o", "gpt-4o", "openai", "gpt"),
        ("gpt-4o-2024-08-06", "gpt-4o-2024-08-06", "openai", "gpt"),
        ("gpt-5.3-codex", "gpt-5.3-codex", "openai", "gpt"),
        ("gpt-6-sol", "gpt-6-sol", "openai", "gpt"),
        ("o3", "o3", "openai", "o"),
        ("o4-mini", "o4-mini", "openai", "o"),
        ("o1-pro", "o1-pro", "openai", "o"),
        ("codex-mini-latest", "codex-mini-latest", "openai", "codex"),
        # google: flash-lite collapses its preview spelling like the other variants
        ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite", "google", "gemini"),
        ("gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite", "google", "gemini"),
        # a distinct upstream model with its own price: the raw id stays the pricing key
        ("gemini-3.1-pro-preview-customtools", "gemini-3.1-pro-preview-customtools", "google", "gemini"),
    ],
)
def test_creator_for_each_shape(raw, name, creator, family):
    parsed = parse_model_name(raw)
    assert (parsed["name"], parsed["creator"], parsed["family"]) == (name, creator, family)


@pytest.mark.parametrize(
    "raw",
    [
        "opus",
        "copilot/auto",
        "copilot/oswe-vscode-prime",
        "big-pickle",
        "MODEL_PLACEHOLDER_M298",
        "<synthetic>",
        "hf.co/mradermacher/Equatorium-v3-12B-i1-GGUF:Q4_K_M",
        "gemma4:e4b",
        "jaahas/qwen3.5-uncensored:9b",
        "gpt-5.4 (xhigh)",  # a display string with a suffix, not an id
        "Gemini 3.7 Flash (High)",
        "text-embedding-3-small",
        "o",  # not an o-series id
        "omlx",
    ],
)
def test_names_that_are_not_a_makers_id_keep_no_creator(raw):
    parsed = parse_model_name(raw)
    assert parsed["name"] == raw
    assert parsed["creator"] is None


@pytest.mark.parametrize(
    "raw,name",
    [
        ("claude-opus-4-5-20251101", "claude-opus-4-5"),
        ("claude-3-5-haiku-20241022", "claude-3-5-haiku"),
        ("claude-3-haiku-20240307", "claude-3-haiku"),
        ("claude-haiku-4.5", "claude-haiku-4-5"),
        ("claude-opus-4-8", "claude-opus-4-8"),
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("gemini-3-pro-preview", "gemini-3-pro"),
        ("gemini-3.1-pro-preview", "gemini-3.1-pro"),
        ("gemini-2.5-flash", "gemini-2.5-flash"),
    ],
)
def test_canonical_names_are_pricing_keys_and_do_not_move(raw, name):
    assert parse_model_name(raw)["name"] == name
