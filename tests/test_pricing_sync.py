"""Pricing sync (scripts/pricing_sync.py): models.dev -> src/siftd/data/pricing.toml.

The sync is a pure transform (``sync(existing_text, upstream, today)``) over a small
fixture JSON shaped like models.dev, so these tests never touch the network. ``main``
is driven with ``--from`` and a temp ``--target``, so the shipped file is never written.
The script is loaded by path, the same way test_probe.py loads scripts/probe.py.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

from siftd.pricing import _parse_toml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SYNC = _REPO_ROOT / "scripts" / "pricing_sync.py"


def _load_sync():
    spec = importlib.util.spec_from_file_location("pricing_sync", _SYNC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules["pricing_sync"] = module
    spec.loader.exec_module(module)
    return module


ps = _load_sync()

TODAY = "2026-10-16"
OLD_DAY = "2026-06-04"
MARKER = ps.MARKER

HEADER = "# header line one\n# header line two\n\n"


def _doc(text: str, marker: bool = True) -> str:
    return HEADER + (MARKER + "\n\n" if marker else "") + text


def _cost(inp, out, cache_read=None, cache_write=None, **extra):
    cost = {"input": inp, "output": out}
    if cache_read is not None:
        cost["cache_read"] = cache_read
    if cache_write is not None:
        cost["cache_write"] = cache_write
    cost.update(extra)
    return {"cost": cost}


def _upstream(**providers):
    return {name: {"models": models} for name, models in providers.items()}


OPUS = _cost(5, 25, 0.5, 6.25)

FIXTURE = _upstream(
    anthropic={
        "claude-opus-4-5": OPUS,
        "claude-opus-4-5-20251101": OPUS,  # dated alias, identical prices
        "claude-sonnet-5": _cost(2, 10),  # no cache published
        "claude-haiku-5-5": _cost(
            0.1,
            0.5,
            0.01,
            0.125,
            tiers=[{"input": 0.5, "output": 2.5, "tier": {"type": "context", "size": 100000}}],
        ),
    },
    openai={
        "gpt-5.5": _cost(5, 30, 0.5, context_over_200k={"input": 10, "output": 45}),
        "text-embedding-3-small": _cost(0.02, 0),
        "no-cost-block": {},  # no cost: out of scope
    },
    google={
        "gemini-3.1-pro-preview": _cost(2, 12, 0.2),
        "gemini-3.1-pro": _cost(2, 12, 0.2),
        "lyria-3-pro-preview": _cost(0, 0),  # billed per song, not per token
    },
    xai={"grok-9": _cost(1, 2)},  # not an owned provider: never synced into the file
)


def _rows(text: str) -> dict[tuple[str, str], dict]:
    return {(r["model"], r["provider"]): r for r in tomllib.loads(text).get("price", [])}


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_dated_alias_and_preview_collapse_onto_canonical_name():
    result = ps.sync(_doc(""), FIXTURE, TODAY)
    rows = _rows(result.text)
    assert ("claude-opus-4-5", "anthropic") in rows
    assert ("claude-opus-4-5-20251101", "anthropic") not in rows
    assert ("gemini-3.1-pro", "google") in rows
    assert ("gemini-3.1-pro-preview", "google") not in rows


def test_collapse_conflict_without_canonical_id_is_refused():
    upstream = _upstream(
        anthropic={
            "claude-haiku-4.5": _cost(1, 5),
            "claude-haiku-4-5-20251001": _cost(2, 10),
        }
    )
    with pytest.raises(ps.SyncError) as exc:
        ps.sync(_doc(""), upstream, TODAY)
    message = str(exc.value)
    assert "claude-haiku-4.5" in message
    assert "claude-haiku-4-5-20251001" in message


def test_collapse_conflict_resolved_by_canonical_id():
    upstream = _upstream(
        anthropic={
            "claude-haiku-4-5": _cost(1, 5),
            "claude-haiku-4-5-20251001": _cost(2, 10),
        }
    )
    rows = _rows(ps.sync(_doc(""), upstream, TODAY).text)
    assert rows[("claude-haiku-4-5", "anthropic")]["input_per_mtok"] == 1.0


# ---------------------------------------------------------------------------
# Written values
# ---------------------------------------------------------------------------


def test_missing_cache_fields_fall_back_to_input_rate():
    rows = _rows(ps.sync(_doc(""), FIXTURE, TODAY).text)
    sonnet = rows[("claude-sonnet-5", "anthropic")]
    assert sonnet["cache_read_per_mtok"] == 2.0
    assert sonnet["cache_creation_per_mtok"] == 2.0


def test_every_synced_entry_writes_all_four_prices_and_provenance():
    rows = _rows(ps.sync(_doc(""), FIXTURE, TODAY).text)
    for key, row in rows.items():
        if row["provider"] not in ps.OWNED_PROVIDERS:
            continue
        for field in ps.PRICE_FIELDS:
            assert isinstance(row[field], float), (key, field)
        assert row["source"] == "https://models.dev/api.json"
        assert row["as_of"] == TODAY


def test_tiered_models_are_counted_and_base_rate_written():
    result = ps.sync(_doc(""), FIXTURE, TODAY)
    rows = _rows(result.text)
    assert rows[("claude-haiku-5-5", "anthropic")]["input_per_mtok"] == 0.1
    assert result.tiered == 2  # haiku tiers + gpt-5.5 context_over_200k
    assert result.modeled == 6  # 3 anthropic (opus collapses two ids), 2 openai, 1 google


def test_a_model_listed_at_zero_input_and_output_is_not_filed_as_free():
    rows = _rows(ps.sync(_doc(""), FIXTURE, TODAY).text)
    assert ("lyria-3-pro-preview", "google") not in rows
    assert ("text-embedding-3-small", "openai") in rows  # zero output alone is a real price


def test_non_owned_provider_models_are_not_synced():
    rows = _rows(ps.sync(_doc(""), FIXTURE, TODAY).text)
    assert all(provider != "xai" for _, provider in rows)


def test_groups_render_in_provider_order_then_others():
    text = ps.sync(
        _doc(
            '[[price]]\nmodel = "grok-9"\nprovider = "xai"\ninput_per_mtok = 1.0\n'
            'output_per_mtok = 2.0\nsource = "hand"\nas_of = "2026-01-01"\n'
        ),
        FIXTURE,
        TODAY,
    ).text
    headers = [line for line in text.splitlines() if line.startswith("# ─── ")]
    assert headers == ["# ─── anthropic ───", "# ─── openai ───", "# ─── google ───", "# ─── xai ───"]


# ---------------------------------------------------------------------------
# Ownership: pins, other providers, unlisted entries
# ---------------------------------------------------------------------------

PINNED_OLD = (
    '[[price]]\nmodel = "claude-opus-4-5"\nprovider = "anthropic"\npinned = true\n'
    'input_per_mtok = 7.0\noutput_per_mtok = 35.0\nsource = "hand-verified"\nas_of = "2026-06-04"\n'
)


def test_pinned_entry_is_never_modified_and_disagreement_is_reported():
    result = ps.sync(_doc(PINNED_OLD), FIXTURE, TODAY)
    row = _rows(result.text)[("claude-opus-4-5", "anthropic")]
    assert row["input_per_mtok"] == 7.0
    assert row["output_per_mtok"] == 35.0
    assert row["source"] == "hand-verified"
    assert row["as_of"] == OLD_DAY
    assert row["pinned"] is True
    assert "claude-opus-4-5" not in " ".join(result.changed)
    assert any("claude-opus-4-5 (anthropic)" in line and "input_per_mtok" in line for line in result.pin_disagreements)


def test_pinned_entry_that_agrees_reports_nothing():
    old = PINNED_OLD.replace("7.0", "5.0").replace("35.0", "25.0")
    result = ps.sync(_doc(old), FIXTURE, TODAY)
    assert result.pin_disagreements == []


def test_pin_disagreement_compares_runtime_derived_cache_rates():
    # A pin that omits the cache fields bills them at input x 0.1 (read) and input x 1.25
    # (creation) at cost time. Upstream publishes no cache rate for this model, so the
    # sync's rule is cache = input (2.0). Runtime bills 0.2, so this is a disagreement.
    old = (
        '[[price]]\nmodel = "claude-sonnet-5"\nprovider = "anthropic"\npinned = true\n'
        'input_per_mtok = 2.0\noutput_per_mtok = 10.0\nsource = "hand"\nas_of = "2026-06-04"\n'
    )
    result = ps.sync(_doc(old), FIXTURE, TODAY)
    assert any("cache_read_per_mtok 0.2 (derived) != upstream" in line for line in result.pin_disagreements)
    assert any("cache_creation_per_mtok 2.5 (derived) != upstream" in line for line in result.pin_disagreements)


def test_owned_entry_omitting_cache_fields_is_unchanged_when_derived_rate_matches():
    # Legacy owned row: no cache fields, so runtime derives read 3.0 x 0.1 (= 0.30000000000000004)
    # and creation 3.0 x 1.25. Upstream publishes 0.3 and 3.75. Same price: no change, no as_of bump.
    upstream = _upstream(anthropic={"claude-sonnet-4-5": _cost(3, 15, 0.3, 3.75)})
    old = (
        '[[price]]\nmodel = "claude-sonnet-4-5"\nprovider = "anthropic"\ninput_per_mtok = 3.0\n'
        'output_per_mtok = 15.0\nsource = "old"\nas_of = "2026-06-04"\n'
    )
    result = ps.sync(_doc(old), upstream, TODAY)
    assert result.changed == {}
    row = _rows(result.text)[("claude-sonnet-4-5", "anthropic")]
    assert row["as_of"] == OLD_DAY
    assert row["cache_read_per_mtok"] == 0.3
    assert row["cache_creation_per_mtok"] == 3.75


def test_other_provider_entries_are_kept_verbatim():
    old = (
        "# verified by hand against the xAI pricing page\n"
        '[[price]]\nmodel = "grok-9"\nprovider = "xai"\ninput_per_mtok = 1.5  # per M, standard tier\n'
        'output_per_mtok = 2.5\ncache_read_per_mtok = 0.3\nsource = "note"\nas_of = "2026-01-02"\n'
    )
    result = ps.sync(_doc(old), FIXTURE, TODAY)
    # Text, not just parsed values: comments and formatting must survive the rewrite.
    assert old in result.text
    row = _rows(result.text)[("grok-9", "xai")]
    assert row == {
        "model": "grok-9",
        "provider": "xai",
        "input_per_mtok": 1.5,
        "output_per_mtok": 2.5,
        "cache_read_per_mtok": 0.3,
        "source": "note",
        "as_of": "2026-01-02",
    }


def test_pinned_entry_text_is_preserved_verbatim():
    pinned = (
        "# hand-verified 2026-06-04 against the Anthropic pricing page\n"
        '[[price]]\nmodel = "claude-opus-4-5"\nprovider = "anthropic"\npinned = true\n'
        "input_per_mtok = 7.0  # pinned above upstream on purpose\n"
        'output_per_mtok = 35.0\nsource = "hand-verified"\nas_of = "2026-06-04"\n'
    )
    result = ps.sync(_doc(pinned), FIXTURE, TODAY)
    assert pinned in result.text


def test_derived_cache_multiples_match_the_runtime_sql():
    # The sync derives omitted cache rates exactly as the cost SQL does. If the SQL
    # multiples move, this fails: the sync and the runtime must agree on the same price.
    sql = (_REPO_ROOT / "src" / "siftd" / "storage" / "sql_helpers.py").read_text(encoding="utf-8")
    assert f"{{input_rate}} * {ps.CACHE_READ_MULTIPLE}" in sql
    assert f"{{input_rate}} * {ps.CACHE_CREATION_MULTIPLE}" in sql


def test_unlisted_owned_entry_is_kept_and_reported():
    old = (
        '[[price]]\nmodel = "claude-3-opus"\nprovider = "anthropic"\ninput_per_mtok = 15.0\n'
        'output_per_mtok = 75.0\nsource = "old"\nas_of = "2026-06-04"\n'
    )
    result = ps.sync(_doc(old), FIXTURE, TODAY)
    assert _rows(result.text)[("claude-3-opus", "anthropic")]["input_per_mtok"] == 15.0
    assert result.kept_unlisted == ["claude-3-opus (anthropic)"]


# ---------------------------------------------------------------------------
# as_of semantics, change report, idempotence
# ---------------------------------------------------------------------------


def test_as_of_preserved_when_unchanged_and_moved_when_changed():
    old = (
        '[[price]]\nmodel = "gpt-5.5"\nprovider = "openai"\ninput_per_mtok = 5.0\n'
        "output_per_mtok = 30.0\ncache_read_per_mtok = 0.5\ncache_creation_per_mtok = 5.0\n"
        'source = "https://developers.openai.com/api/docs/pricing"\nas_of = "2026-06-04"\n'
    )
    result = ps.sync(_doc(old), FIXTURE, TODAY)
    row = _rows(result.text)[("gpt-5.5", "openai")]
    assert row["as_of"] == OLD_DAY
    assert result.changed == {}  # cache_creation 5.0 is the upstream value, no change

    repriced = old.replace("output_per_mtok = 30.0", "output_per_mtok = 28.0")
    result = ps.sync(_doc(repriced), FIXTURE, TODAY)
    assert _rows(result.text)[("gpt-5.5", "openai")]["as_of"] == TODAY
    assert result.changed == {"gpt-5.5 (openai)": ["output_per_mtok 28.0 -> 30.0"]}


def test_added_entries_are_reported_and_stamped():
    result = ps.sync(_doc(""), FIXTURE, TODAY)
    assert "claude-sonnet-5 (anthropic) 2.0/10.0" in result.added
    assert _rows(result.text)[("claude-sonnet-5", "anthropic")]["as_of"] == TODAY


def test_second_run_is_a_no_op():
    first = ps.sync(_doc(PINNED_OLD), FIXTURE, TODAY)
    second = ps.sync(first.text, FIXTURE, "2026-12-31")
    assert second.text == first.text
    assert second.added == []
    assert second.changed == {}
    assert second.provenance_rewritten == 0


def test_output_parses_with_the_runtime_loader():
    text = ps.sync(_doc(PINNED_OLD), FIXTURE, TODAY).text
    parsed = _parse_toml(text)
    assert parsed[("claude-opus-4-5", "anthropic")].input_per_mtok == 7.0
    assert parsed[("claude-haiku-5-5", "anthropic")].cache_read_per_mtok == 0.01


def test_header_above_marker_is_preserved_and_marker_required():
    text = ps.sync(_doc(""), FIXTURE, TODAY).text
    assert text.startswith(HEADER)
    with pytest.raises(ps.SyncError, match="marker"):
        ps.sync(_doc("", marker=False), FIXTURE, TODAY)


# ---------------------------------------------------------------------------
# CLI: exit codes and file effects
# ---------------------------------------------------------------------------


def _write_inputs(tmp_path: Path, existing: str) -> tuple[Path, Path]:
    import json

    target = tmp_path / "pricing.toml"
    target.write_text(existing, encoding="utf-8")
    source = tmp_path / "models_dev.json"
    source.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return target, source


def test_check_writes_nothing_and_exits_one_when_file_would_change(tmp_path, capsys):
    target, source = _write_inputs(tmp_path, _doc(""))
    before = target.read_text(encoding="utf-8")
    code = ps.main(["--from", str(source), "--target", str(target), "--check", "--as-of", TODAY])
    assert code == 1
    assert target.read_text(encoding="utf-8") == before
    assert "would change" in capsys.readouterr().out


def test_check_exits_zero_when_current(tmp_path):
    target, source = _write_inputs(tmp_path, _doc(""))
    assert ps.main(["--from", str(source), "--target", str(target), "--as-of", TODAY]) == 0
    assert ps.main(["--from", str(source), "--target", str(target), "--check", "--as-of", TODAY]) == 0


def test_run_writes_then_second_run_leaves_file_byte_identical(tmp_path):
    target, source = _write_inputs(tmp_path, _doc(""))
    assert ps.main(["--from", str(source), "--target", str(target), "--as-of", TODAY]) == 0
    once = target.read_text(encoding="utf-8")
    assert ps.main(["--from", str(source), "--target", str(target), "--as-of", "2027-01-01"]) == 0
    assert target.read_text(encoding="utf-8") == once


def test_conflict_exits_two_and_leaves_file_untouched(tmp_path, capsys):
    import json

    target = tmp_path / "pricing.toml"
    original = _doc("")
    target.write_text(original, encoding="utf-8")
    source = tmp_path / "models_dev.json"
    source.write_text(
        json.dumps(_upstream(anthropic={"claude-haiku-4.5": _cost(1, 5), "claude-haiku-4-5-20251001": _cost(2, 10)})),
        encoding="utf-8",
    )
    code = ps.main(["--from", str(source), "--target", str(target), "--as-of", TODAY])
    assert code == 2
    assert target.read_text(encoding="utf-8") == original
    assert "claude-haiku-4.5" in capsys.readouterr().err
