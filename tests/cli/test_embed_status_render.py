"""Rendering tests for 'siftd embed --status' states (base lane — synthetic status).

Drives ``cli.embed._embed_status`` against a hand-built ``EmbedIndexStatus`` so the tri-
state honesty (findings 2/3) is exercised without loading a real embedding backend:
empty→"not built" (never a rebuild / "vNone"), populated-v1→"v1", and a configured/stored
backend mismatch→actionable warning.
"""

import argparse
import json
from pathlib import Path

from siftd.cli.embed import _embed_status
from siftd.embeddings.indexer import EmbedIndexStatus


def _status(**over) -> EmbedIndexStatus:
    base = dict(
        configured_backend="fastembed",
        configured_usable=True,
        configured_reason="local fastembed backend installed",
        index_exists=True,
        needs_rebuild=False,
        stored_backend="fastembed",
        stored_model="BAAI/bge-small-en-v1.5",
        stored_dimension=384,
        schema_version=2,
        strategy="exchange-window",
        built_at="2026-07-04T00:00:00Z",
        total_chunks=10,
        backend_mismatch=False,
        stored_backend_config="fastembed",
        chunk_counts={"exchange": 10},
        conversations_indexed=5,
        conversations_total=5,
        conversations_stale=0,
        db_size_bytes=2048,
    )
    base.update(over)
    return EmbedIndexStatus(**base)


def _args(**over) -> argparse.Namespace:
    d = dict(db=None, embed_db=None, rebuild=False, status=True, json=False)
    d.update(over)
    return argparse.Namespace(**d)


def _run(monkeypatch, capsys, report):
    monkeypatch.setattr("siftd.api.embed_status", lambda **_k: report)
    rc = _embed_status(_args(), Path("/x/main.db"), Path("/x/embed.db"))
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def test_empty_index_renders_not_built_never_rebuild(monkeypatch, capsys):
    rc, text = _run(monkeypatch, capsys, _status(total_chunks=0, conversations_stale=5))
    assert rc == 0
    assert "not built" in text
    assert "outdated" not in text and "vNone" not in text and "rebuild" not in text.lower()


def test_populated_v1_renders_v1_not_vnone(monkeypatch, capsys):
    rc, text = _run(monkeypatch, capsys, _status(needs_rebuild=True, schema_version=None))
    assert "outdated (v1)" in text
    assert "vNone" not in text


def test_backend_mismatch_renders_actionable_warning(monkeypatch, capsys):
    rc, text = _run(
        monkeypatch,
        capsys,
        _status(backend_mismatch=True, stored_backend="fake", configured_backend="remote:voyage", stored_backend_config="fake"),
    )
    assert "different backend" in text
    assert "embed.backend = fake" in text


def test_status_json_serializes_new_fields(monkeypatch, capsys):
    monkeypatch.setattr(
        "siftd.api.embed_status",
        lambda **_k: _status(backend_mismatch=True, stored_backend_config="voyage"),
    )
    rc = _embed_status(_args(json=True), Path("/x/main.db"), Path("/x/embed.db"))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["backend_mismatch"] is True
    assert data["stored_backend_config"] == "voyage"
    assert data["schema_version"] == 2
