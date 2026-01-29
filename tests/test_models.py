"""Tests for model name parsing."""

import pytest

from siftd.models import parse_model_name


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
