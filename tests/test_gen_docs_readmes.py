"""Tests for the readmes target of scripts/gen_docs.py (marker engine + strict mode).

gen_docs.py lives under scripts/ (not the siftd package), so it is loaded by
path the same way the script bootstraps itself. These tests stay in the base
lane: they exercise the pure marker engine on fixture text plus the real
manifest's idempotency, and never require optional deps.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GEN_DOCS = _REPO_ROOT / "scripts" / "gen_docs.py"


def _load_gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", _GEN_DOCS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen_docs = _load_gen_docs()


# --- marker engine: splice_markers -------------------------------------------


def test_splice_replaces_only_marked_span():
    text = (
        "# Title\n\n"
        "Authored prose that must survive.\n\n"
        "<!-- gen:begin modules -->\nOLD BODY\n<!-- gen:end -->\n\n"
        "Footer prose.\n"
    )
    out = gen_docs.splice_markers(text, {"modules": "NEW BODY"}, source="x")
    assert "Authored prose that must survive." in out
    assert "Footer prose." in out
    assert "OLD BODY" not in out
    assert "<!-- gen:begin modules -->\nNEW BODY\n<!-- gen:end -->" in out


def test_splice_is_idempotent():
    text = (
        "intro\n<!-- gen:begin a -->\n<!-- gen:end -->\nmid\n"
        "<!-- gen:begin b -->\n<!-- gen:end -->\nend\n"
    )
    bodies = {"a": "AAA", "b": "BBB"}
    once = gen_docs.splice_markers(text, bodies, source="x")
    twice = gen_docs.splice_markers(once, bodies, source="x")
    assert once == twice


def test_splice_preserves_authored_prose_byte_for_byte():
    prose_before = "line1\n  indented\n\ttabbed\n"
    prose_after = "trailing prose with `code` and | pipes\n"
    text = (
        prose_before
        + "<!-- gen:begin s -->\nwhatever\n<!-- gen:end -->\n"
        + prose_after
    )
    out = gen_docs.splice_markers(text, {"s": "BODY"}, source="x")
    assert out.startswith(prose_before)
    assert out.endswith(prose_after)


def test_splice_unknown_id_errors():
    text = "<!-- gen:begin nope -->\n<!-- gen:end -->\n"
    with pytest.raises(ValueError, match="unknown gen section id"):
        gen_docs.splice_markers(text, {"known": "x"}, source="x")


def test_splice_unclosed_marker_errors():
    text = "<!-- gen:begin s -->\nno end here\n"
    with pytest.raises(ValueError, match="unclosed"):
        gen_docs.splice_markers(text, {"s": "x"}, source="x")


def test_splice_nested_marker_errors():
    text = "<!-- gen:begin a -->\n<!-- gen:begin b -->\n<!-- gen:end -->\n<!-- gen:end -->\n"
    with pytest.raises(ValueError, match="nested"):
        gen_docs.splice_markers(text, {"a": "x", "b": "y"}, source="x")


def test_splice_stray_end_errors():
    text = "prose\n<!-- gen:end -->\n"
    with pytest.raises(ValueError, match="without a matching"):
        gen_docs.splice_markers(text, {}, source="x")


def test_splice_missing_marker_errors():
    text = "<!-- gen:begin a -->\n<!-- gen:end -->\n"
    with pytest.raises(ValueError, match="missing gen markers"):
        gen_docs.splice_markers(text, {"a": "x", "b": "y"}, source="x")


def test_splice_duplicate_id_errors():
    text = (
        "<!-- gen:begin a -->\n<!-- gen:end -->\n"
        "<!-- gen:begin a -->\n<!-- gen:end -->\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        gen_docs.splice_markers(text, {"a": "x"}, source="x")


# --- manifest integrity + real idempotency -----------------------------------


def test_manifest_paths_exist():
    for entry in gen_docs.MANIFEST:
        assert (_REPO_ROOT / entry.path).exists(), f"missing managed README: {entry.path}"


def test_committed_readmes_are_idempotent():
    """Filling each committed README's spans reproduces the committed bytes."""
    for entry in gen_docs.MANIFEST:
        text = (_REPO_ROOT / entry.path).read_text()
        assert gen_docs.fill_readme(entry, text) == text, (
            f"{entry.path} is stale — run ./dev docs"
        )


# --- strict mode --------------------------------------------------------------


def test_strict_mode_fails_on_skipped_target(monkeypatch):
    """A target that degrades to a skip is a hard failure under --strict."""
    monkeypatch.setattr(gen_docs, "generate_api_docs", lambda: None)
    assert gen_docs.run(["api"], strict=True) == 1
    # Same skip is tolerated without --strict (graceful degradation).
    assert gen_docs.run(["api"], strict=False) == 0
