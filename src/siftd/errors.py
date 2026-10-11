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
  renders the message and exits — 2 for ``UserInputError``, 1 otherwise.
  serve has one app-level handler (``serve/app.py``) that answers every route
  with ``http_status`` and ``to_dict()``, never matching class names.
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

from http import HTTPStatus


class SiftdError(Exception):
    """Base for errors whose message is complete and user-actionable.

    Anything that escapes to the CLI backstop renders as a clean error line,
    never a traceback. Prefer a branch below when one fits (input vs drift);
    operation failures that are neither — a sync push that failed, a resource
    copy that didn't — join this root directly (exit 1, wire 500).
    """

    # Root default: generic server failure; branches refine (400/503). serve's
    # app-level handler answers with it for every class in the taxonomy.
    http_status: int = 500

    # Whether ``str(self)`` is written for a remote caller as well as the
    # operator, so serve may send it. Default-deny: a message naming server
    # paths, secrets' locations or upstream responses must never reach the wire
    # because a class forgot to opt out. A class opts in; a raise site whose
    # text is not safe sets it False on the instance.
    wire_message: bool = False

    # Optional remediation NEXT STEP, rendered by the CLI backstop on
    # status.error's hint channel. Distinct from the message (the fact +
    # inline remediation): set it only when the follow-up is a separate
    # command the user could run (e.g. AnchorNotFound suggesting a search).
    # CLI-only — the serve body is to_dict() and drops it.
    hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        """The serve wire body for this error (#72).

        The message when ``wire_message`` allows it, else the status phrase
        ("service unavailable"); serve logs the full message either way. A
        subclass with structure to carry, or a fixed caller-safe message,
        overrides this.
        """
        if self.wire_message:
            return {"error": str(self)}
        return {"error": HTTPStatus(self.http_status).phrase.lower()}


class UserInputError(SiftdError):
    """The request itself is malformed or unresolvable — bad flag values,
    out-of-range anchors, ambiguous ID prefixes, queries that cannot parse.

    Presentation: CLI exit 2 (argparse convention for usage errors, matching
    the anchor-error precedent), serve HTTP 400.
    """

    http_status = 400
    # The request is the caller's own; the message is about it.
    wire_message = True


class DriftError(SiftdError):
    """The environment has drifted from what the operation needs — index
    schema skew, backend/model mismatch, a stale database schema, unusable
    config. Terminal for this invocation but remediable; the message names
    the remediation.

    Presentation: CLI exit 1, serve HTTP 503.
    """

    http_status = 503
