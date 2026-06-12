"""Litestar application factory for siftd serve."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar.datastructures import MutableScopeHeaders
from litestar.di import Provide
from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware
from litestar.static_files import create_static_files_router

from siftd.serve.html_routes import (
    ui_auth_config,
    ui_dashboard,
    ui_export,
    ui_find,
    ui_folio,
    ui_follow,
    ui_meta,
    ui_query,
    ui_search,
    ui_sessions,
    ui_shell,
    ui_tag,
    ui_tags_suggest,
    ui_view_stub,
)
from siftd.serve.routes import (
    conversation_detail,
    conversation_list,
    event_detail_route,
    export_route,
    health,
    index,
    pull,
    push,
    search_route,
    session_queue_tag_route,
    stats_route,
    sync_status_route,
    tag_write_route,
    tags_route,
    workspace_detail_route,
    workspaces_route,
)


def _origin(url: str) -> str | None:
    """Return ``scheme://host[:port]`` for a URL, or None if it has no origin."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def _build_csp(auth_config: dict | None) -> str:
    """Build the Content-Security-Policy header value (finding F3).

    All JS (htmx, prism) is vendored under ``/static`` so there is no external
    script origin to compromise. ``script-src`` keeps ``'unsafe-inline'`` for
    the shell's inline blocks + ``onclick`` handlers; removing it (nonces) is a
    tracked follow-up. We deliberately omit ``'unsafe-eval'`` — htmx's
    ``hx-on`` compiles via ``new Function`` which the policy blocks, so those
    were rewritten as listeners (see the shell + the browser CSP smoke).

    ``connect-src`` is the key control: even after a hypothetical script
    injection, a bearer token in sessionStorage could not be exfiltrated to an
    off-origin endpoint. But the browser SSO flow (``auth.js``) discovers the
    IdP via ``fetch(issuer/.well-known/openid-configuration)`` and exchanges the
    auth code via ``fetch(token_endpoint)`` — both governed by ``connect-src``.
    So when an OIDC issuer is configured we widen ``connect-src`` to that
    origin; otherwise client-side login would silently fail. The authorize
    *redirect* is navigation, not connect-src, so it needs no allowance.
    HSTS is left to the TLS-terminating reverse proxy (Caddy).
    """
    connect = "'self'"
    issuer = (auth_config or {}).get("issuer") if auth_config else None
    if issuer:
        origin = _origin(str(issuer))
        if origin:
            connect = f"'self' {origin}"
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        f"connect-src {connect}; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )


class _SecurityHeadersMiddleware(ASGIMiddleware):
    """Attach defense-in-depth security headers to every HTTP response (F3).

    This is per-app ASGI middleware rather than an ``after_request`` hook
    because Litestar memoizes route-handler resolution (including the resolved
    ``after_request``) process-wide the first time any app serves a path —
    with a hook, once one app has served ``GET /``, every later
    ``create_app()`` instance's HTTP responses inherit the *first* app's CSP.
    Middleware instances are per-app, so each app's headers stay its own.

    Known parity gap (same as the hook it replaced): responses synthesized
    from middleware-raised exceptions (auth 401s, rate-limit 429s) carry no
    security headers — Litestar converts those to responses outside the user
    middleware stack, so no list position can intercept them. Verified
    empirically; acceptable because those are bare JSON error bodies with no
    scriptable content.
    """

    scopes = (ScopeType.HTTP,)

    def __init__(self, csp: str) -> None:
        self.csp = csp

    async def handle(self, scope: Any, receive: Any, send: Any, next_app: Any) -> None:
        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableScopeHeaders.from_message(message)
                headers.setdefault("Content-Security-Policy", self.csp)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
            await send(message)

        await next_app(scope, receive, send_with_headers)


def create_app(
    *,
    db_path: Path,
    auth_config: dict | None = None,
    fts_rebuild: str = "on_push",
    request_max_body_size: int = 500_000_000,  # 500 MB in SI bytes (matches Caddy default)
    rate_limit_per_minute: int | None = 600,
    allow_live_endpoints: bool = True,
) -> Litestar:
    """Create the Litestar application.

    Args:
        db_path: Path to the team SQLite database.
        auth_config: Auth config dict (None = no auth).
        fts_rebuild: FTS rebuild strategy ("on_push", "scheduled", "off").
        request_max_body_size: Max request body in bytes; applies to streaming push.
        rate_limit_per_minute: Per-client request cap (finding F4). ``None``/``0``
            disables it. The limiter keys by the real client IP — honoring
            ``X-Forwarded-For`` from configured trusted proxies — so it is
            effective behind a reverse proxy that would otherwise collapse every
            client to one address. ``/static`` and the health check are exempt.
            The generous default (600/min) does not impede the htmx UI's polling
            but throttles credential brute force.
        allow_live_endpoints: When False, the ``/follow`` live session endpoint
            is not registered and the Sessions view renders its ingested zone
            only (finding F7). Live session data is read from the *server
            host's* filesystem, bypassing DB owner scoping, so it must be off
            on a shared/public deployment.
    """

    async def provide_db_path() -> Path:
        return db_path

    async def provide_fts_rebuild() -> str:
        return fts_rebuild

    async def provide_request_max_body_size() -> int:
        return request_max_body_size

    async def provide_auth_config() -> dict | None:
        return auth_config

    async def provide_live_enabled() -> bool:
        return allow_live_endpoints

    middleware: list[Any] = [_SecurityHeadersMiddleware(csp=_build_csp(auth_config))]
    if auth_config:
        from siftd.serve.auth import create_auth_middleware, validate_auth_config

        validate_auth_config(auth_config)  # fail loudly at boot, not per-request
        middleware.append(create_auth_middleware(auth_config))

    if rate_limit_per_minute:
        from litestar.middleware.rate_limit import RateLimitConfig

        from siftd.serve.routes import _client_ip

        def _rate_id(request: Any) -> str:
            # Key by the real client IP (trusted-proxy XFF aware) so the limit
            # isn't collapsed to the reverse proxy's single address.
            return _client_ip(request) or "unknown"

        rate_config = RateLimitConfig(
            rate_limit=("minute", rate_limit_per_minute),
            exclude=["^/static", "^/api/v1/health$"],
            identifier_for_request=_rate_id,
        )
        middleware.append(rate_config.middleware)

    static_dir = Path(__file__).parent / "static"
    static_router = create_static_files_router(path="/static", directories=[static_dir])

    route_handlers: list[Any] = [
        index, health, stats_route, workspaces_route, workspace_detail_route,
        tag_write_route, session_queue_tag_route, tags_route,
        export_route,
        push, pull, sync_status_route, conversation_detail, conversation_list,
        event_detail_route, search_route,
        ui_shell, ui_auth_config, ui_folio, ui_dashboard, ui_sessions,
        ui_view_stub,
        ui_find, ui_meta, ui_query, ui_search,
        ui_tag, ui_tags_suggest, ui_export,
        static_router,
    ]
    if allow_live_endpoints:
        route_handlers.append(ui_follow)

    return Litestar(
        route_handlers=route_handlers,
        dependencies={
            "db_path": Provide(provide_db_path),
            "fts_rebuild": Provide(provide_fts_rebuild),
            "request_max_body_size": Provide(provide_request_max_body_size),
            "auth_config": Provide(provide_auth_config),
            "live_enabled": Provide(provide_live_enabled),
        },
        middleware=middleware,
        request_max_body_size=request_max_body_size,
    )
