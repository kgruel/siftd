# siftd.content

A small, focused package: binary-content detection and filtering applied during ingestion. [filters.py](filters.py) recognizes embedded images, PDFs, and large base64 or magic-byte binary payloads inside content blocks and replaces them with metadata-only placeholders. This keeps the database compact and the FTS index clean without discarding the surrounding searchable text.

These are pure functions with no storage or domain dependencies — they take content in and return filtered content out, and are invoked from the ingest/store path rather than owning any state. When adjusting detection (for example the base64 length threshold, which is tuned to avoid flagging JWTs and hashes), remember the goal is to drop non-searchable bytes while preserving context, not to alter the conversation structure.

See [Storage — Database size](../../../docs/concepts/storage.md#database-size) for where binary filtering fits in the overall size story.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [filters.py](filters.py) | Binary content detection and filtering. |
<!-- gen:end -->
