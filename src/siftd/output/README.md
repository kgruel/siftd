# siftd.output

This package turns already-fetched domain data into rendered output; it does not
query. [`format_registry.py`](format_registry.py) discovers formatters — built-in
(terminal, markdown, JSON, HTML), user drop-ins, and entry-point plugins — each
implementing the `OutputFormat` protocol (`render_detail` / `render_list` /
`render_search`, dispatched on the returned type). The terminal formatter renders
through [painted](painted_bridge.py) primitives; `theme.py`, `gutter.py`,
`row.py`, and `table.py` are its visual vocabulary.

Formatters are route- and transport-agnostic on purpose: they must not hardcode
serve URLs (enforced by `test_output_formatters_no_hardcoded_routes` in
[`tests/architecture/test_hard_rules.py`](../../../tests/architecture/test_hard_rules.py)),
and link bases arrive as render-context kwargs from the caller. One boundary to
keep in mind: the serve layer cannot import this package (importing the terminal
formatter would pull painted into the server), so serve's JSON emission lives in
its twin, [`serialization/`](../serialization/) — keep the two `--json` shapes in
agreement when you touch `json_fmt.py`.

<!-- gen:begin modules -->
<sub>generated from module docstrings — run <code>./dev docs</code></sub>

| Module | Summary |
|--------|---------|
| [_id_format.py](_id_format.py) | — |
| [common.py](common.py) | Common formatting utilities for CLI output. |
| [format_registry.py](format_registry.py) | Output format registry: discovers built-in, drop-in, and entry point output formatters. |
| [gutter.py](gutter.py) | The grain gutter — a per-line left-margin mark encoding a narrative line's kind. |
| [help.py](help.py) | One help grammar for every CLI surface — root, branch, and leaf. |
| [html_fmt.py](html_fmt.py) | HTML fragment output format — renders conversations as htmx-swappable fragments. |
| [json_fmt.py](json_fmt.py) | JSON output format — renders conversations as structured JSON. |
| [listing.py](listing.py) | Report-structure atoms — an underlined section heading and a key:value listing. |
| [live.py](live.py) | Live-render policy over painted's ``InPlaceRenderer``. |
| [mark.py](mark.py) | The brand mark — the ``sift▪d`` wordmark. |
| [markdown_fmt.py](markdown_fmt.py) | Markdown output format — renders conversations as GFM-compatible markdown. |
| [markdown_render.py](markdown_render.py) | Render markdown transcript bodies onto painted Lines/Blocks for the terminal. |
| [narrative.py](narrative.py) | Presentation-layer emitters for narrative rendering. |
| [painted_bridge.py](painted_bridge.py) | Bridge normalized narrative data onto painted rendering primitives. |
| [progress_view.py](progress_view.py) | The generic ``ProgressEvent`` consumer — one renderer for every action bar. |
| [row.py](row.py) | The row atom — a styled line of text segments. |
| [status.py](status.py) | Status vocabulary — themed CLI status / notice output. |
| [table.py](table.py) | Canonical CLI table rendering — one width-budgeted painted table. |
| [terminal_fmt.py](terminal_fmt.py) | Terminal output format — renders via painted Block/Line/Span primitives. |
| [theme.py](theme.py) | Visual language — domain styles composed from painted primitives. |
| [tool_presenters.py](tool_presenters.py) | Format-neutral tool content extraction. |
| [validation.py](validation.py) | Public validation utilities for formatter modules. |
<!-- gen:end -->
