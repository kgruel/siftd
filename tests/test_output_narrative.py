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
        '<details class="thinking" open>',
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
