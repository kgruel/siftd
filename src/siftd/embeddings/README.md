# siftd.embeddings

This package holds the embedding backends behind semantic search. Backend
resolution is config-driven off `[embed].backend` (see `base.py` for the
deterministic resolver and `availability.py` for `embedding_status()`): either a
remote OpenAI-compatible provider, which needs only an API key, or the local
`fastembed` ONNX backend, which needs the optional `[embed]` extra
(`fastembed`, `onnxruntime`, `tokenizers`, `huggingface-hub`).

Mind the dependency boundary. `numpy` — the vector math for cosine scoring and
MMR — is a *base* dependency, so hybrid search against a remote backend works
without the `[embed]` extra; only the local ONNX inference chain is extra-gated.
The package `__init__` re-exports only the light availability/status layer;
heavier backend and indexer symbols are imported from their concrete submodules
(`base.py`, `indexer.py`) so vector-math imports stay off the hot CLI paths.
`tests/architecture/test_hard_rules.py` enforces this lazy-import discipline.

Because the local backend is optional, its tests carry
`pytestmark = pytest.mark.embeddings` and guard imports with
`pytest.importorskip("fastembed")` — they run only in the embeddings lane
(`./dev test-embed` / `./dev test-all`). The base-lane edge tests
(`test_embeddings_base_edges.py`, `test_embeddings_remote_edges.py`) exercise
resolution and the remote backend with fakes and no fastembed installed.

Provider presets are reference data, not code: `data/embed_presets.toml`
(loaded by `presets.py`) supplies per-provider model/dimension defaults for the
remote backend. For the conceptual model of hybrid search and the index
lifecycle see `docs/concepts/search.md`; the user-facing build/rebuild/status
flow is the `siftd embed` command.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [availability.py](availability.py) | Embedding availability — is a backend *configured/installed*? |
| [base.py](base.py) | Embedding backend protocol, exception taxonomy, and deterministic resolution. |
| [chunker.py](chunker.py) | Token-aware text chunking for embeddings. |
| [egress.py](egress.py) | Remote first-egress disclosure — text + shown-once persistence. |
| [fastembed_backend.py](fastembed_backend.py) | FastEmbed backend — local ONNX embeddings, no network required. |
| [indexer.py](indexer.py) | Embeddings index builder — schema-v2 fingerprint lifecycle. |
| [presets.py](presets.py) | Embedding provider presets — reference data for the remote backend. |
| [remote.py](remote.py) | Generic OpenAI-compatible remote embedding backend. |
<!-- gen:end -->
