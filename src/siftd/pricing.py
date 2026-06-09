"""Pricing reference — the source of truth for model prices.

A price is reference data (a fact about ``(model, provider)``, identical on every
machine, sourced externally), not per-machine user data. The shipped reference
``siftd/data/pricing.toml`` is the canonical list; ``~/.config/siftd/pricing.toml``
overrides it per ``(model, provider)`` for corrections/additions without a code change.

The ``pricing`` SQLite table is a *projection* of ``load_pricing_reference()``,
UPSERT-applied on every DB open by ``storage.sqlite.ensure_pricing_table`` — so
correcting a value here (or in the override) fixes the table on the next open, and
nothing is ever frozen. See ``siftd/data/pricing.toml`` for the file format.
"""

import importlib.resources
import tomllib
from typing import NamedTuple

from siftd.paths import pricing_override_file


class PriceEntry(NamedTuple):
    """One model price, with provenance. Cache rates are optional overrides."""

    model: str  # canonical model name (matches models.name)
    provider: str
    input_per_mtok: float | None
    output_per_mtok: float | None
    cache_read_per_mtok: float | None
    cache_creation_per_mtok: float | None
    source: str | None
    as_of: str | None


def _entry_from_row(row: dict) -> PriceEntry:
    return PriceEntry(
        model=row["model"],
        provider=row["provider"],
        input_per_mtok=row.get("input_per_mtok"),
        output_per_mtok=row.get("output_per_mtok"),
        cache_read_per_mtok=row.get("cache_read_per_mtok"),
        cache_creation_per_mtok=row.get("cache_creation_per_mtok"),
        source=row.get("source"),
        as_of=row.get("as_of"),
    )


def _parse_toml(text: str) -> dict[tuple[str, str], PriceEntry]:
    """Parse pricing TOML into a {(model, provider): entry} map.

    Skips entries missing the ``model``/``provider`` key (keyed by both).
    """
    doc = tomllib.loads(text)
    out: dict[tuple[str, str], PriceEntry] = {}
    for row in doc.get("price", []):
        if "model" not in row or "provider" not in row:
            continue
        entry = _entry_from_row(row)
        out[(entry.model, entry.provider)] = entry
    return out


def _load_shipped() -> dict[tuple[str, str], PriceEntry]:
    ref = importlib.resources.files("siftd").joinpath("data/pricing.toml")
    return _parse_toml(ref.read_text(encoding="utf-8"))


def _load_override() -> dict[tuple[str, str], PriceEntry]:
    path = pricing_override_file()
    if not path.exists():
        return {}
    return _parse_toml(path.read_text(encoding="utf-8"))


def load_pricing_reference() -> list[PriceEntry]:
    """Return the effective price list: shipped reference, overridden per
    ``(model, provider)`` by the user file. The user override wins.
    """
    merged = _load_shipped()
    merged.update(_load_override())
    return list(merged.values())
