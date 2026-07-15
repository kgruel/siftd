"""Exception taxonomy: the presentation contract for user-facing errors.

This is a leaf module (imports nothing from the package) so every layer — storage, embeddings,
adapters, api, cli, serve — can import it without violating the layered
architecture (tests/architecture/test_imports.py). Only the taxonomy *bases*
live here; concrete exceptions stay in the module that owns their domain and
inherit one of these.

The contract a subclass signs:

- ``str(e)`` is a complete, user-actionable message. The concrete class owns
  the message template and builds it from structured context in ``__init__``
  (or a classmethod constructor when one error kind has several raise sites);
  raise sites pass data, not prose.
- Boundaries catch the *bases*. The CLI backstop (``cli/__init__.py main()``)
  renders the message and exits — 2 for ``UserInputError``, 1 otherwise. The
  serve dispatch maps branches to status codes instead of matching class
  names as strings.
- Carry structured attributes only when a consumer reads them (wire
  serialization, exit-code logic, test assertions). A plain message is fine
  otherwise — same contract, not same ceremony.

Deliberately OUTSIDE the taxonomy (enforced by
tests/architecture/test_exceptions.py):

- Invariant violations (``BlobCollisionError``, ``MigrationAssertionError``) —
  they signal bugs, and a traceback is the correct rendering for a bug.
- Transient / control-flow signals (``EmbeddingTransientError``,
  ``ServeUnavailable``, ``ServeRequest4xx``) — they are handled (degrade,
  fall back, surface structurally), never presented as terminal errors.
"""

from __future__ import annotations


class SiftdError(Exception):
    """Base for errors whose message is complete and user-actionable.

    Anything that escapes to the CLI backstop renders as a clean error line,
    never a traceback. Subclass one of the two branches below rather than
    this root; the root exists so boundaries can catch the whole taxonomy
    in one clause.
    """

    # Root default: generic server failure; branches refine (400/503). A direct
    # root joiner (e.g. a future slice's operation-family exception) keeps
    # today's generic-500 wire behavior instead of AttributeError-ing serve's
    # `except SiftdError: ... e.http_status` dispatch.
    http_status: int = 500


class UserInputError(SiftdError):
    """The request itself is malformed or unresolvable — bad flag values,
    out-of-range anchors, ambiguous ID prefixes, queries that cannot parse.

    Presentation: CLI exit 2 (argparse convention for usage errors, matching
    the anchor-error precedent), serve HTTP 400.
    """

    http_status = 400


class DriftError(SiftdError):
    """The environment has drifted from what the operation needs — index
    schema skew, backend/model mismatch, a stale database schema, unusable
    config. Terminal for this invocation but remediable; the message names
    the remediation.

    Presentation: CLI exit 1, serve HTTP 503.
    """

    http_status = 503
