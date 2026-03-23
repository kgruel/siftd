import io
import json
import urllib.error

import pytest

from siftd.embeddings.ollama_backend import OllamaBackend


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_list_models_and_find_model_priority(monkeypatch):
    def fake_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/tags"):
            return _Resp({"models": [{"name": "abc"}, {"name": "nomic-embed-text:latest"}]})
        if url.endswith("/api/embeddings"):
            return _Resp({"embedding": [0.1, 0.2, 0.3]})
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    b = OllamaBackend(base_url="http://x")
    assert b.model == "nomic-embed-text:latest" and b.dimension == 3


def test_find_model_embed_fallback_and_no_model(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/tags"):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp({"models": [{"name": "some-embed-model"}]})
            return _Resp({"models": [{"name": "plain-model"}]})
        if url.endswith("/api/embeddings"):
            return _Resp({"embedding": [1.0]})
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    b = OllamaBackend(base_url="http://x")
    assert b.model == "some-embed-model"

    with pytest.raises(RuntimeError, match="No embedding model found"):
        OllamaBackend(base_url="http://x")


def test_embed_one_and_embed_batch_and_error_paths(monkeypatch):
    def fake_urlopen(req, timeout=5):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/tags"):
            return _Resp({"models": [{"name": "nomic-embed-text"}]})
        if url.endswith("/api/embeddings"):
            body = json.loads(req.data.decode())
            if body["prompt"] == "badjson":
                return _Resp({"x": 1})
            if body["prompt"] == "urlerr":
                raise urllib.error.URLError("down")
            return _Resp({"embedding": [float(len(body["prompt"]))]})
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    b = OllamaBackend(base_url="http://x")
    assert b.embed(["a", "bc"]) == [[1.0], [2.0]]
    assert b.embed_one("xyz") == [3.0]

    with pytest.raises(RuntimeError, match="Ollama embed failed"):
        b.embed_one("badjson")
    with pytest.raises(RuntimeError, match="Ollama embed failed"):
        b.embed_one("urlerr")


def test_list_models_connection_and_json_errors(monkeypatch):
    def raise_url(*_a, **_k):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr("urllib.request.urlopen", raise_url)
    b = object.__new__(OllamaBackend)
    b.base_url = "http://x"
    with pytest.raises(ConnectionError, match="Cannot connect to Ollama"):
        b._list_models()

    def bad_json(*_a, **_k):
        return io.BytesIO(b"not-json")

    monkeypatch.setattr("urllib.request.urlopen", bad_json)
    with pytest.raises(ConnectionError, match="Cannot connect to Ollama"):
        b._list_models()
