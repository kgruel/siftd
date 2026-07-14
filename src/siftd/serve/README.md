# siftd.serve

<!-- TODO(preamble): authored in slice 3 -->
HTTP server (optional [serve] extra) — routes, auth, htmx UI.

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
