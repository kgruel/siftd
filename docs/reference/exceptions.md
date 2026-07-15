# Exception Reference

_Auto-generated from `src/siftd/` and `tests/architecture/test_exceptions.py`._

The taxonomy bases live in `src/siftd/errors.py`; concrete exceptions stay in
the module that owns their domain and join a branch by inheritance. The
contract: `str(e)` is a complete, user-actionable message built by the class
from structured context — raise sites pass data, not prose. Boundaries catch
the bases: the CLI backstop (`cli/__init__.py main()`) renders the message and
exits, and serve dispatch maps `e.http_status` to a response — so a new
exception that joins the taxonomy is handled everywhere without touching a
catch site.

## Taxonomy map

```
SiftdError (exit 1, HTTP 500) — errors.py
├── UserInputError (exit 2, HTTP 400) — errors.py
│   ├── AmbiguousPrefix — api/conversations.py
│   ├── AnchorError — api/conversations.py
│   │   ├── AnchorNotFound — api/conversations.py
│   │   ├── AnchorOutOfRange — api/conversations.py
│   │   └── AnchorPhraseInvalid — api/conversations.py
│   ├── QueryError — api/conversations.py
│   ├── AdapterSelectionError — api/ingest.py
│   ├── EmbeddingsRequiredError — api/search.py
│   └── AmbiguousSessionError — peek/reader.py
├── DriftError (exit 1, HTTP 503) — errors.py
│   ├── EmbeddingsNotAvailable  (HTTP 501) — embeddings/availability.py
│   ├── EmbeddingConfigError — embeddings/base.py
│   ├── IncrementalCompatError — embeddings/indexer.py
│   ├── IndexCompatError — storage/embeddings.py
│   └── SchemaUpgradeRequiredError — storage/sqlite.py
├── AdapterParseError — adapters/sdk.py
├── AuthError — api/auth.py
├── PreflightError — api/database.py
├── CopyError — api/resources.py
├── SyncError — api/sync.py
├── AuthLoginError — credentials.py
└── TokenRefError — credentials.py
```

## Members

| Class | Module | Branch | Purpose |
|-------|--------|--------|---------|
| `AmbiguousPrefix` | `api/conversations.py` | UserInputError | Prefix matches multiple targets — caller must use a longer prefix or full ID |
| `AnchorError` | `api/conversations.py` | UserInputError | Raised when an anchor cannot be resolved during get_conversation |
| `AnchorNotFound` | `api/conversations.py` | UserInputError | --around PHRASE matched nothing in this conversation |
| `AnchorOutOfRange` | `api/conversations.py` | UserInputError | --at-turn N is out of range for this conversation |
| `AnchorPhraseInvalid` | `api/conversations.py` | UserInputError | --around PHRASE could not be parsed by FTS5 |
| `QueryError` | `api/conversations.py` | UserInputError | Error running a SQL query file |
| `AdapterSelectionError` | `api/ingest.py` | UserInputError | Raised when requested adapter names match no discovered adapters |
| `EmbeddingsRequiredError` | `api/search.py` | UserInputError | Raised when an explicit ``semantic``/``hybrid`` mode is requested but embeddings are unavailable |
| `AmbiguousSessionError` | `peek/reader.py` | UserInputError | Raised when a session ID prefix matches multiple files |
| `EmbeddingsNotAvailable` | `embeddings/availability.py` | DriftError | Raised when embedding functionality is requested but no backend is available |
| `EmbeddingConfigError` | `embeddings/base.py` | DriftError | [embed] config is present but unusable — bad backend name, unresolvable key ref, or a preset missing a required model/base_url |
| `IncrementalCompatError` | `embeddings/indexer.py` | DriftError | Raised when an incremental build cannot proceed against the existing index |
| `IndexCompatError` | `storage/embeddings.py` | DriftError | Raised when index metadata is incompatible with current backend configuration |
| `SchemaUpgradeRequiredError` | `storage/sqlite.py` | DriftError | Raised on read-only open of a stale-schema DB that cannot be auto-upgraded |
| `AdapterParseError` | `adapters/sdk.py` | SiftdError (root) | Raised when a source matches an adapter but cannot be parsed safely |
| `AuthError` | `api/auth.py` | SiftdError (root) | Raised when token acquisition fails |
| `PreflightError` | `api/database.py` | SiftdError (root) | Raised when a source database fails integrity pre-flight checks |
| `CopyError` | `api/resources.py` | SiftdError (root) | Error copying a resource |
| `SyncError` | `api/sync.py` | SiftdError (root) | Raised when a sync operation fails |
| `AuthLoginError` | `credentials.py` | SiftdError (root) | Raised when interactive token acquisition (`siftd auth login`) fails |
| `TokenRefError` | `credentials.py` | SiftdError (root) | Raised when an ``env:``/``file:`` token reference cannot be resolved |

## Outside the taxonomy (permanent carve-outs)

These are excluded by design and enforced by the ratchet — a traceback or a
structural handling path is the *correct* behavior for them, not a bug:

| Class | Module | Why it stays out |
|-------|--------|------------------|
| `MissingOpSpec` | `api/op_spec.py` | invariant violation (op registered without wire spec); also caught as delegation control flow |
| `EmbeddingError` | `embeddings/base.py` | domain grouping base, not a taxonomy member — EmbeddingTransientError subclasses it and must not transitively reach SiftdError (would break degrade-to-fts); EmbeddingConfigError joins DriftError directly instead |
| `EmbeddingTransientError` | `embeddings/base.py` | degradable blip; search falls back to FTS |
| `ServeRequest4xx` | `serve/client.py` | delegation control flow (structured 4xx surface) |
| `ServeUnavailable` | `serve/client.py` | delegation control flow (fall back to local) |
| `BlobCollisionError` | `storage/blobs.py` | invariant violation (SHA256 collision) |
| `MigrationAssertionError` | `storage/sqlite.py` | invariant violation (migration assertion) |

## Enforcement

`tests/architecture/test_exceptions.py` is the ratchet: every exception
defined under `src/siftd/` must join the taxonomy or sit on an explicit
allowlist, and classes merely *named* like exceptions must classify as one.
A new exception that joins nothing fails CI with a pointer here.
