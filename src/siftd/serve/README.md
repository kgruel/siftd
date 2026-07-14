# siftd.serve

This package is the HTTP server, available only under the optional `[serve]`
extra. [`app.py`](app.py) is the Litestar application factory;
[`routes.py`](routes.py) and [`html_routes.py`](html_routes.py) are the JSON and
htmx handlers. Like the CLI, routes reach the database only through the
[`api/`](../api/) layer — importing `siftd.storage` directly is rejected by
`test_serve_no_direct_storage_import`
([`tests/architecture/test_hard_rules.py`](../../../tests/architecture/test_hard_rules.py))
— and they serialize responses via [`serialization/`](../serialization/), not
[`output/`](../output/), which the server must never import.

Authentication is split across two namespaces, and the distinction matters when
you touch either side: [`auth.py`](auth.py) here is the *validate* side —
middleware that checks incoming tokens (static token, OIDC/JWKS, or RFC 7662
introspection) per the `serve.auth` config, installed only when auth is
configured. The *send* side (acquiring and attaching a token as a client) lives
in `api/auth.py` under the `[auth]` config namespace. Separately,
[`delegation.py`](delegation.py) is what lets a plain CLI invocation transparently
forward to a running server; it depends on no `[serve]` extras and uses the
stdlib-only [`client.py`](client.py) for transport. See
[docs/concepts/serve.md](../../../docs/concepts/serve.md) for the operator view
and the [delegation contract](../../../docs/guides/delegation-contract.md) for
the wire shape.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [app.py](app.py) | Litestar application factory for siftd serve. |
| [auth.py](auth.py) | Authentication middleware for siftd serve. |
| [client.py](client.py) | Stdlib-only HTTP client for talking to a running siftd-serve. |
| [delegation.py](delegation.py) | Serve delegation policy — transparently delegate CLI commands to siftd-serve. |
| [html_routes.py](html_routes.py) | HTML route handlers for htmx-driven web UI. |
| [routes.py](routes.py) | Route handlers for siftd serve. |
<!-- gen:end -->
