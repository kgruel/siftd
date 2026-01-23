"""Model name parsing utilities.

Decomposes raw model identifiers (e.g. 'claude-opus-4-5-20251101')
into structured fields: name, creator, family, version, variant, released.
"""

import re

# Claude variants and their position patterns
_CLAUDE_VARIANTS = {"opus", "sonnet", "haiku"}

# Gemini variants
_GEMINI_VARIANTS = {"pro", "flash", "ultra"}


def parse_model_name(raw_name: str) -> dict:
    """Parse a raw model name into structured fields.

    Returns dict with keys: name, creator, family, version, variant, released.
    Unknown patterns return name=raw_name with everything else None.
    """
    if raw_name.startswith("claude-"):
        return _parse_claude(raw_name)
    elif raw_name.startswith("gemini-"):
        return _parse_gemini(raw_name)
    else:
        return _fallback(raw_name)


def _parse_claude(raw_name: str) -> dict:
    """Parse Claude model names.

    Two patterns:
      claude-{variant}-{major}-{minor}-{YYYYMMDD}   (4.x series)
      claude-{major}-{minor}-{variant}-{YYYYMMDD}   (3.x series)
      claude-{major}-{variant}-{YYYYMMDD}            (3 without minor)
    """
    # Pattern 1: claude-{variant}-{major}-{minor}-{YYYYMMDD}
    m = re.match(
        r"^claude-(" + "|".join(_CLAUDE_VARIANTS) + r")-(\d+)-(\d+)-(\d{8})$",
        raw_name,
    )
    if m:
        variant, major, minor, date_str = m.groups()
        version = f"{major}.{minor}"
        name = f"claude-{variant}-{major}-{minor}"
        released = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return {
            "name": name,
            "creator": "anthropic",
            "family": "claude",
            "version": version,
            "variant": variant,
            "released": released,
        }

    # Pattern 2: claude-{major}-{minor}-{variant}-{YYYYMMDD}
    m = re.match(
        r"^claude-(\d+)-(\d+)-(" + "|".join(_CLAUDE_VARIANTS) + r")-(\d{8})$",
        raw_name,
    )
    if m:
        major, minor, variant, date_str = m.groups()
        version = f"{major}.{minor}"
        name = f"claude-{major}-{minor}-{variant}"
        released = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return {
            "name": name,
            "creator": "anthropic",
            "family": "claude",
            "version": version,
            "variant": variant,
            "released": released,
        }

    # Pattern 3: claude-{major}-{variant}-{YYYYMMDD} (no minor version)
    m = re.match(
        r"^claude-(\d+)-(" + "|".join(_CLAUDE_VARIANTS) + r")-(\d{8})$",
        raw_name,
    )
    if m:
        major, variant, date_str = m.groups()
        version = major
        name = f"claude-{major}-{variant}"
        released = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return {
            "name": name,
            "creator": "anthropic",
            "family": "claude",
            "version": version,
            "variant": variant,
            "released": released,
        }

    return _fallback(raw_name)


def _parse_gemini(raw_name: str) -> dict:
    """Parse Gemini model names.

    Pattern: gemini-{version}-{variant}[-preview]
    Version can be "3", "2.5", etc.
    """
    # gemini-{version}-{variant}[-preview]
    m = re.match(
        r"^gemini-(\d+(?:\.\d+)?)-(" + "|".join(_GEMINI_VARIANTS) + r")(-preview)?$",
        raw_name,
    )
    if m:
        version, variant, preview = m.groups()
        name = f"gemini-{version}-{variant}"
        return {
            "name": name,
            "creator": "google",
            "family": "gemini",
            "version": version,
            "variant": variant,
            "released": None,
        }

    return _fallback(raw_name)


def _fallback(raw_name: str) -> dict:
    """Fallback for unrecognized patterns."""
    return {
        "name": raw_name,
        "creator": None,
        "family": None,
        "version": None,
        "variant": None,
        "released": None,
    }
