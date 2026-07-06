"""Embedding provider presets — reference data for the remote backend.

A preset is reference data (a fact about a provider's OpenAI-compatible embeddings
endpoint, identical on every machine, sourced from provider docs), not per-machine user
data. The shipped ``siftd/data/embed_presets.toml`` is the canonical list; code dispatches
only on the ``intent_style`` enum, so adding a provider is a data edit — see that file for
the format. Mirrors ``siftd/pricing.py``'s shipped-reference loader.
"""

from __future__ import annotations

import importlib.resources
import tomllib
from typing import NamedTuple

# Query/document asymmetry handling. The RemoteBackend maps intent (document|query) to a
# provider-native mechanism based on this enum.
INTENT_STYLES = frozenset({"none", "param:input_type", "param:task", "prefix"})


class EmbedPreset(NamedTuple):
    """One provider preset: the defaults the remote client needs to reach it."""

    name: str
    base_url: str  # "" for `custom` — requires embed.base_url
    default_model: str | None  # None for ollama/custom — requires embed.model
    default_dimensions: int | None  # None ⇒ learn from first response
    intent_style: str
    max_batch: int
    dimensions_param: str  # wire field for truncation (OpenAI `dimensions`, Voyage/Mistral `output_dimension`)


def _preset_from_row(row: dict) -> EmbedPreset:
    intent_style = str(row.get("intent_style") or "none")
    if intent_style not in INTENT_STYLES:
        raise ValueError(
            f"embed preset {row.get('name')!r}: unknown intent_style {intent_style!r} "
            f"(valid: {', '.join(sorted(INTENT_STYLES))})"
        )
    return EmbedPreset(
        name=str(row["name"]),
        base_url=str(row.get("base_url") or ""),
        default_model=(str(row["default_model"]) if row.get("default_model") else None),
        default_dimensions=(
            int(row["default_dimensions"]) if row.get("default_dimensions") is not None else None
        ),
        intent_style=intent_style,
        max_batch=int(row.get("max_batch") or 64),
        dimensions_param=str(row.get("dimensions_param") or "dimensions"),
    )


def _parse_toml(text: str) -> dict[str, EmbedPreset]:
    """Parse presets TOML into a {name: preset} map. Skips entries missing ``name``."""
    doc = tomllib.loads(text)
    out: dict[str, EmbedPreset] = {}
    for row in doc.get("preset", []):
        if "name" not in row:
            continue
        preset = _preset_from_row(row)
        out[preset.name] = preset
    return out


def load_presets() -> dict[str, EmbedPreset]:
    """Return the shipped preset map, keyed by preset name."""
    ref = importlib.resources.files("siftd").joinpath("data/embed_presets.toml")
    return _parse_toml(ref.read_text(encoding="utf-8"))


def get_preset(name: str) -> EmbedPreset | None:
    """Return the preset named ``name``, or None if it is not a known preset."""
    return load_presets().get(name)


def preset_names() -> list[str]:
    """Return the sorted list of known preset names (for error messages/validation)."""
    return sorted(load_presets())
