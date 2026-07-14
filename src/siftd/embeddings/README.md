# siftd.embeddings

<!-- TODO(preamble): authored in slice 3 -->
Semantic search (optional [embed] extra).

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
