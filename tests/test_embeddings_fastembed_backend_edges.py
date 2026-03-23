import sys
import types

import pytest

from siftd.embeddings.fastembed_backend import FastEmbedBackend


def test_constructor_importerror_message(monkeypatch):
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    orig_import = __import__

    def fake_import(name, *a, **k):
        if name == "fastembed":
            raise ImportError("missing")
        return orig_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ImportError, match="siftd install embed"):
        FastEmbedBackend()


def test_embed_and_embed_one_and_dimension_probe(monkeypatch):
    class _Vec:
        def __init__(self, vals):
            self._vals = vals

        def tolist(self):
            return self._vals

    class _TextEmbedding:
        def __init__(self, model_name):
            self.model_name = model_name

        def embed(self, texts):
            return (_Vec([float(len(t)), 1.0]) for t in texts)

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=_TextEmbedding))

    b = FastEmbedBackend(model="demo/model")
    assert b.name == "fastembed" and b.model == "demo/model" and b.dimension == 2
    assert b.embed(["a", "abcd"]) == [[1.0, 1.0], [4.0, 1.0]]
    assert b.embed_one("xyz") == [3.0, 1.0]


def test_probe_dimension_uses_embed_one(monkeypatch):
    class _TextEmbedding:
        def __init__(self, model_name):
            self.model_name = model_name

        def embed(self, texts):
            return iter([types.SimpleNamespace(tolist=lambda: [0.0, 0.1, 0.2])])

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=_TextEmbedding))
    b = FastEmbedBackend()
    assert b._probe_dimension() == 3
