"""Tests for model name parsing."""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tbd.models import parse_model_name


# --- Claude 4.x pattern: claude-{variant}-{major}-{minor}-{YYYYMMDD} ---

def test_claude_opus_4_5():
    result = parse_model_name("claude-opus-4-5-20251101")
    assert result == {
        "name": "claude-opus-4-5",
        "creator": "anthropic",
        "family": "claude",
        "version": "4.5",
        "variant": "opus",
        "released": "2025-11-01",
    }


def test_claude_haiku_4_5():
    result = parse_model_name("claude-haiku-4-5-20251001")
    assert result == {
        "name": "claude-haiku-4-5",
        "creator": "anthropic",
        "family": "claude",
        "version": "4.5",
        "variant": "haiku",
        "released": "2025-10-01",
    }


def test_claude_sonnet_4_5():
    result = parse_model_name("claude-sonnet-4-5-20250929")
    assert result == {
        "name": "claude-sonnet-4-5",
        "creator": "anthropic",
        "family": "claude",
        "version": "4.5",
        "variant": "sonnet",
        "released": "2025-09-29",
    }


# --- Claude 3.x pattern: claude-{major}-{minor}-{variant}-{YYYYMMDD} ---

def test_claude_3_5_haiku():
    result = parse_model_name("claude-3-5-haiku-20241022")
    assert result == {
        "name": "claude-3-5-haiku",
        "creator": "anthropic",
        "family": "claude",
        "version": "3.5",
        "variant": "haiku",
        "released": "2024-10-22",
    }


# --- Claude 3 pattern: claude-{major}-{variant}-{YYYYMMDD} ---

def test_claude_3_haiku():
    result = parse_model_name("claude-3-haiku-20240307")
    assert result == {
        "name": "claude-3-haiku",
        "creator": "anthropic",
        "family": "claude",
        "version": "3",
        "variant": "haiku",
        "released": "2024-03-07",
    }


# --- Gemini patterns ---

def test_gemini_3_pro_preview():
    result = parse_model_name("gemini-3-pro-preview")
    assert result == {
        "name": "gemini-3-pro",
        "creator": "google",
        "family": "gemini",
        "version": "3",
        "variant": "pro",
        "released": None,
    }


def test_gemini_3_flash_preview():
    result = parse_model_name("gemini-3-flash-preview")
    assert result == {
        "name": "gemini-3-flash",
        "creator": "google",
        "family": "gemini",
        "version": "3",
        "variant": "flash",
        "released": None,
    }


def test_gemini_2_5_pro():
    result = parse_model_name("gemini-2.5-pro")
    assert result == {
        "name": "gemini-2.5-pro",
        "creator": "google",
        "family": "gemini",
        "version": "2.5",
        "variant": "pro",
        "released": None,
    }


# --- Fallback ---

def test_fallback_synthetic():
    result = parse_model_name("<synthetic>")
    assert result == {
        "name": "<synthetic>",
        "creator": None,
        "family": None,
        "version": None,
        "variant": None,
        "released": None,
    }


def test_fallback_unknown():
    result = parse_model_name("some-unknown-model-v2")
    assert result == {
        "name": "some-unknown-model-v2",
        "creator": None,
        "family": None,
        "version": None,
        "variant": None,
        "released": None,
    }
