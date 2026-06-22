from types import SimpleNamespace

from siftd.output.narrative import HtmlEmitter, MarkdownEmitter


def test_markdown_emitter_core_paths():
    e = MarkdownEmitter()
    e.tool_summary([("read", 2, None), ("grep", 1, None)])
    e.tool_content("read", 2, "x" * 120 + "\nrest", "y" * 250, "error")
    e.tool_output("tool_result", "ok")
    out = "\n".join(e.lines)
    assert "*[read ×2, grep]*" in out and "- **read** ×2 (error)" in out and "..." in out and "```\nok\n```" in out


def test_html_emitter_full_branch_sweep(monkeypatch):
    assert HtmlEmitter._lang_from_path("file.py") == "python"
    assert HtmlEmitter._lang_from_path("file.unknown") == ""
    assert HtmlEmitter._lang_for_tool("shell.execute") == "bash"

    e = HtmlEmitter()
    e.thinking("thinking")
    e.thinking_placeholder()
    e.tool_summary([("grep", 2, "error"), ("read", 1, "success")])

    for pres in [
        SimpleNamespace(headline="src/main.py", meta="m", removed="old", added="new", output="out", overflow=3, error="err", tasks=[("a", True), ("b", False)]),
        SimpleNamespace(headline="f.txt", meta=None, removed="old", added=None, output=None, overflow=0, error=None, tasks=[("x", True)]),
        SimpleNamespace(headline="f.txt", meta=None, removed=None, added="new", output=None, overflow=0, error=None, tasks=[("x", True)]),
        SimpleNamespace(headline="h", meta="meta", removed=None, added=None, output=None, overflow=0, error=None, tasks=[]),
    ]:
        monkeypatch.setattr("siftd.output.tool_presenters.extract_tool_presentation", lambda *a, _pres=pres, **k: _pres)
        e.tool_content("file.edit" if pres.headline.endswith(".py") else "grep", 2 if pres.headline.endswith(".py") else 1, None, None, "error" if pres.error else None)
    e.tool_output("tool_result", "ok")

    import builtins
    import sys

    monkeypatch.setitem(sys.modules, "mistune", SimpleNamespace(create_markdown=lambda escape=True: (lambda c: f"<em>{c}</em>")))
    e.text("rich")
    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError("no mistune")) if name == "mistune" else real_import(name, *a, **k))
    e.text("para1\n\npara2")

    html = e.to_html()
    for frag in [
        '<details class="thinking">',
        "thinking placeholder",
        "tool-summary",
        '<details class="tool-call tool-error">',
        "diff-pair",
        "tool-overflow",
        "task-done",
        "task-pending",
        'tool-diff tool-removed',
        'tool-diff tool-added',
        '<div class="tool-call">',
        '<span class="tool-meta">meta</span>',
        '<pre class="tool-result">ok</pre>',
        '<div class="narrative-text"><em>rich</em></div>',
        '<p class="narrative-text">para1</p>',
        '<p class="narrative-text">para2</p>',
    ]:
        assert frag in html


def test_html_emitter_anchors_once_per_event_run():
    # A response = many blocks under one event_id → exactly one data-event-id for
    # it (a unique anchor / jump target, not one per block). A new event_id opens
    # a new anchor; a None id anchors nothing. target_event_id marks the matched
    # run .is-target.
    e = HtmlEmitter(target_event_id="01B")
    e.text("a1", event_id="01A")
    e.text("a2 same run", event_id="01A")   # same event → no second anchor
    e.text("b1", event_id="01B")            # new event → new anchor + is-target
    e.thinking("b-think", event_id="01B")   # still 01B → no extra anchor
    e.text("loose", event_id=None)          # no id → no anchor
    html = e.to_html()
    assert html.count('data-event-id="01A"') == 1
    assert html.count('data-event-id="01B"') == 1
    assert html.count("data-event-id") == 2
    assert html.count("is-target") == 1
    assert 'class="narrative-text is-target" data-event-id="01B"' in html


def test_html_emitter_escapes_event_id():
    # The route validates ?event= to a ULID charset, but the emitter still escapes
    # defensively — an event_id can never break out of the attribute.
    e = HtmlEmitter()
    e.text("x", event_id='a"b<c')
    html = e.to_html()
    assert 'data-event-id="a"b<c"' not in html
    assert "a&quot;b&lt;c" in html


def test_html_emitter_anchors_tool_first_run(monkeypatch):
    # A response whose FIRST emitted block is a tool call (no preceding prose — a
    # common agent shape) must anchor on the tool element itself, not silently
    # skip the run. Guards the anchor branch on tool_content/tool_output, which
    # the text-driven run test never reaches.
    pres = SimpleNamespace(
        headline="x.py", meta=None, removed=None, added=None,
        output="result body", overflow=0, error=None, tasks=[],
    )
    monkeypatch.setattr(
        "siftd.output.tool_presenters.extract_tool_presentation",
        lambda *a, **k: pres,
    )
    e = HtmlEmitter(target_event_id="01T")
    e.tool_content("Read", 1, "x.py", "result body", "success", event_id="01T")
    e.tool_output("tool_result", "standalone", event_id="01TO")
    html = e.to_html()
    # The tool-call container (first block of run 01T) is anchored + targeted.
    assert 'data-event-id="01T"' in html
    assert "is-target" in html
    # A standalone tool_output opening a new run is anchored too.
    assert 'class="tool-result" data-event-id="01TO"' in html
