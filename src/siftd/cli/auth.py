"""CLI for client-side token acquisition: `siftd auth login/status/logout`.

These commands ACQUIRE and manage a bearer the CLI presents to a remote
`siftd serve`. serve itself only validates tokens — see credentials.py and
docs/dev/plans/2026-05-27-serve-auth-direction.md.
"""

from __future__ import annotations

import sys

from siftd.output import status


def _require_issuer() -> str | None:
    from siftd.config import get_config

    issuer = get_config("auth.issuer")
    if not issuer:
        from painted import print_block

        from siftd.output.common import should_use_ansi
        from siftd.output.listing import lines

        # An enumerated-remedy error: the two `config set` commands ride a
        # lines() block — a callout's hint flattens newlines and can't carry a
        # multi-line body. All to stderr so a piped stdout stays clean.
        status.error(
            "No [auth].issuer configured.",
            hint="Configure device-code login first:",
        )
        print_block(
            lines(
                [
                    "siftd config set auth.issuer https://idp.example.com/...",
                    "siftd config set auth.client_id <public-device-code-client-id>",
                ]
            ),
            sys.stderr,
            use_ansi=should_use_ansi(sys.stderr),
        )
        return None
    return str(issuer)


def _fmt_expiry(expires_at: float | None) -> str:
    if expires_at is None:
        return "unknown expiry"
    import datetime

    when = datetime.datetime.fromtimestamp(expires_at).astimezone()
    return f"expires {when:%Y-%m-%d %H:%M %Z}"


def cmd_login(args) -> int:
    from siftd.credentials import AuthLoginError, device_login

    issuer = _require_issuer()
    if not issuer:
        return 1
    try:
        cred = device_login(issuer)
    except AuthLoginError as e:
        status.error(f"Login failed: {e}")
        return 1
    status.confirm(f"Logged in to {issuer} ({_fmt_expiry(cred.expires_at)}).")
    return 0


def cmd_status(args) -> int:
    from siftd.credentials import load

    issuer = _require_issuer()
    if not issuer:
        return 1
    cred = load(issuer)
    if cred is None:
        status.error(f"Not logged in to {issuer}.", hint="Run `siftd auth login`.")
        return 1
    state = "stale (will refresh on next use)" if cred.is_stale() else "valid"
    has_refresh = "yes" if cred.refresh_token else "no"
    from siftd.output.listing import StatusReport
    from siftd.paths import credential_file

    report = StatusReport()
    report.preamble(
        {
            "Issuer": issuer,
            "Status": f"{state} ({_fmt_expiry(cred.expires_at)})",
            "Refreshable": has_refresh,
            "Stored at": str(credential_file(issuer)),
        }
    )
    report.render()
    return 0


def cmd_logout(args) -> int:
    from siftd.credentials import delete

    issuer = _require_issuer()
    if not issuer:
        return 1
    if delete(issuer):
        status.confirm(f"Logged out of {issuer}.")
    else:
        status.info(f"No stored credential for {issuer}.")
    return 0


def build_auth_parser(subparsers) -> None:
    """Register the `auth` subcommand group (login / status / logout)."""
    parser = subparsers.add_parser(
        "auth",
        help="Acquire and manage a bearer token for a remote siftd serve",
        description=(
            "Client-side token acquisition. `login` runs the OAuth device-code "
            "flow against the configured [auth].issuer; the resulting token is "
            "stored and presented automatically to a remote siftd serve."
        ),
    )
    sub = parser.add_subparsers(dest="auth_command")

    p_login = sub.add_parser("login", help="Authorize via OAuth device-code flow")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="Show stored credential status")
    p_status.set_defaults(func=cmd_status)

    p_logout = sub.add_parser("logout", help="Delete the stored credential")
    p_logout.set_defaults(func=cmd_logout)

    def _auth_default(args) -> int:
        parser.print_help()
        return 0

    parser.set_defaults(func=_auth_default)
