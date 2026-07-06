"""RemoteBackend edge cases — fake httpx transport, no network (base lane).

Covers batching, per-style intent application, auth header, dimensions passthrough,
retry/backoff (429 Retry-After + 5xx), the config-vs-transient exception taxonomy, and
dimension-learned-from-first-response.
"""

import json

import httpx
import pytest

from siftd.embeddings.base import EmbeddingConfigError, EmbeddingTransientError
from siftd.embeddings.remote import RemoteBackend


def make_backend(handler, *, sleeps=None, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    params = dict(
        preset_name="test",
        base_url="https://api.example.com/v1",
        model="test-model",
        intent_style="none",
        max_batch=64,
        api_key="key",
        client=client,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )
    params.update(kwargs)
    return RemoteBackend(**params)


def _ok(request, dim=3):
    inputs = json.loads(request.content)["input"]
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0] * dim} for i in range(len(inputs))]})


def test_batches_split_at_max_batch_preserving_order():
    requests = []

    def handler(request):
        requests.append(request)
        return _ok(request)

    b = make_backend(handler, max_batch=2)
    texts = ["a", "b", "c", "d", "e"]
    out = b.embed_documents(texts)

    assert len(out) == 5
    assert len(requests) == 3  # 2 + 2 + 1
    seen = [t for r in requests for t in json.loads(r.content)["input"]]
    assert seen == texts
    assert all(len(json.loads(r.content)["input"]) <= 2 for r in requests)


def test_bearer_header_sent():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return _ok(request)

    make_backend(handler).embed_query("hi")
    assert captured["auth"] == "Bearer key"


def test_intent_input_type_param():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok(request)

    b = make_backend(handler, intent_style="param:input_type")
    b.embed_documents(["doc"])
    b.embed_query("q")
    assert bodies[0]["input_type"] == "document"
    assert bodies[1]["input_type"] == "query"


def test_intent_task_param():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok(request)

    b = make_backend(handler, intent_style="param:task")
    b.embed_documents(["doc"])
    b.embed_query("q")
    assert bodies[0]["task"] == "retrieval.passage"
    assert bodies[1]["task"] == "retrieval.query"


def test_intent_prefix_style():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok(request)

    b = make_backend(handler, intent_style="prefix", document_prefix="DOC: ", query_prefix="Q: ")
    b.embed_documents(["x"])
    b.embed_query("y")
    assert bodies[0]["input"] == ["DOC: x"]
    assert bodies[1]["input"] == ["Q: y"]


def test_dimensions_passthrough_only_when_configured():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok(request)

    make_backend(handler).embed_query("a")
    make_backend(handler, dimensions_param=256).embed_query("b")
    assert "dimensions" not in bodies[0]
    assert bodies[1]["dimensions"] == 256


def test_no_encoding_format_in_body():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok(request)

    make_backend(handler).embed_query("a")
    # Voyage rejects encoding_format="float"; every provider defaults to float when omitted.
    assert "encoding_format" not in bodies[0]


def test_dimensions_param_uses_preset_field_name():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok(request)

    make_backend(handler, dimensions_param=512, dimensions_param_name="output_dimension").embed_query("a")
    assert bodies[0]["output_dimension"] == 512
    assert "dimensions" not in bodies[0]


def test_dimension_learned_from_first_response():
    b = make_backend(lambda r: _ok(r, dim=7), dimension=None)
    assert b.dimension is None
    b.embed_documents(["x"])
    assert b.dimension == 7


def test_dimension_mismatch_raises_config_error():
    # Declared dimension 3, provider returns 5 (ignored the truncation param) ⇒ hard error
    # rather than silently storing ragged vectors.
    b = make_backend(lambda r: _ok(r, dim=5), dimension=3)
    with pytest.raises(EmbeddingConfigError, match="expected dimension 3 but .* returned 5"):
        b.embed_documents(["x"])


def test_short_response_raises_transient():
    # A 200 with fewer rows than inputs is a malformed/buggy server — transient (retried
    # or degraded), never silently truncated: a short batch would drop a chunk yet still
    # stamp the shorted conversation's fingerprint as current, hiding the gap permanently.
    def handler(request):
        inputs = json.loads(request.content)["input"]
        rows = [{"index": i, "embedding": [1.0] * 3} for i in range(len(inputs) - 1)]
        return httpx.Response(200, json={"data": rows})

    b = make_backend(handler, dimension=3)
    with pytest.raises(EmbeddingTransientError, match="expected 3 embeddings, got 2"):
        b.embed_documents(["a", "b", "c"])


def test_retry_on_429_honors_retry_after():
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return _ok(request)

    b = make_backend(handler, sleeps=sleeps)
    out = b.embed_query("x")
    assert len(out) == 3
    assert sleeps == [2.0]


def test_retry_on_5xx_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, json={})
        return _ok(request)

    b = make_backend(handler, sleeps=sleeps, backoff_base=1.0)
    b.embed_query("x")
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_persistent_5xx_raises_transient():
    b = make_backend(lambda r: httpx.Response(500, json={}))
    with pytest.raises(EmbeddingTransientError, match="HTTP 500"):
        b.embed_query("x")


def test_timeout_raises_transient():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    b = make_backend(handler)
    with pytest.raises(EmbeddingTransientError, match="request failed"):
        b.embed_query("x")


def test_401_raises_config_error():
    b = make_backend(lambda r: httpx.Response(401, json={}))
    with pytest.raises(EmbeddingConfigError, match="authentication failed"):
        b.embed_query("x")


def test_bad_request_raises_config_error():
    b = make_backend(lambda r: httpx.Response(400, text="bad model"))
    with pytest.raises(EmbeddingConfigError, match="request rejected"):
        b.embed_query("x")


def test_empty_input_short_circuits():
    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("no request expected for empty input")

    assert make_backend(handler).embed_documents([]) == []


def test_ollama_preset_url_shape(monkeypatch):
    """The ollama preset points the client at the OpenAI-compat /v1 endpoint."""
    import siftd.config as config
    from siftd.embeddings import base

    values = {"embed.backend": "ollama", "embed.model": "nomic-embed-text"}
    monkeypatch.setattr(config, "get_config", lambda key: values.get(key))
    base.invalidate_backend_cache()

    b = base.resolve_backend()
    assert b.name == "remote:ollama"
    assert b._url == "http://localhost:11434/v1/embeddings"
