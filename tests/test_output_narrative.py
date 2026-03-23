from types import SimpleNamespace

from siftd.output.narrative import HtmlEmitter, MarkdownEmitter


def test_markdown_emitter_tool_summary_content_and_output():
    e = MarkdownEmitter()
    e.tool_summary([("read", 2, None), ("grep", 1, None)])
    e.tool_content("read", 2, "x" * 120 + "\nrest", "y" * 250, "error")
    e.tool_output("tool_result", "ok")

    out = "\n".join(e.lines)
    assert "*[read ×2, grep]*" in out
    assert "- **read** ×2 (error)" in out
    assert "..." in out
    assert "```\nok\n```" in out


def test_html_emitter_helpers_and_tool_content_branches(monkeypatch):
    assert HtmlEmitter._lang_from_path("file.py") == "python"
    assert HtmlEmitter._lang_from_path("file.unknown") == ""
    assert HtmlEmitter._lang_for_tool("shell.execute") == "bash"

    e = HtmlEmitter()
    e.thinking("thinking")
    e.thinking_placeholder()
    e.tool_summary([("grep", 2, "error"), ("read", 1, "success")])

    pres = SimpleNamespace(
        headline="src/main.py",
        meta="m",
        removed="old",
        added="new",
        output="out",
        overflow=3,
        error="err",
        tasks=[("a", True), ("b", False)],
    )
    monkeypatch.setattr("siftd.output.tool_presenters.extract_tool_presentation", lambda *a, **k: pres)

    e.tool_content("file.edit", 2, "in", "res", "error")

    # removed-only and added-only diff branches
    pres_removed = SimpleNamespace(headline="f.txt", meta=None, removed="old", added=None, output=None, overflow=0, error=None, tasks=[("x", True)])
    monkeypatch.setattr("siftd.output.tool_presenters.extract_tool_presentation", lambda *a, **k: pres_removed)
    e.tool_content("edit", 1, None, None, None)
    pres_added = SimpleNamespace(headline="f.txt", meta=None, removed=None, added="new", output=None, overflow=0, error=None, tasks=[("x", True)])
    monkeypatch.setattr("siftd.output.tool_presenters.extract_tool_presentation", lambda *a, **k: pres_added)
    e.tool_content("edit", 1, None, None, None)

    # has_content false + summary_meta branch
    pres2 = SimpleNamespace(headline="h", meta="meta", removed=None, added=None, output=None, overflow=0, error=None, tasks=[])
    monkeypatch.setattr("siftd.output.tool_presenters.extract_tool_presentation", lambda *a, **k: pres2)
    e.tool_content("grep", 1, None, None, None)
    e.tool_output("tool_result", "ok")

    html = e.to_html()
    assert "<details class=\"thinking\" open>" in html
    assert "thinking placeholder" in html and "tool-summary" in html
    assert "<details class=\"tool-call tool-error\">" in html
    assert "diff-pair" in html and "tool-overflow" in html and "task-done" in html and "task-pending" in html
    assert 'tool-diff tool-removed' in html and 'tool-diff tool-added' in html
    assert '<div class="tool-call">' in html and '<span class="tool-meta">meta</span>' in html
    assert '<pre class="tool-result">ok</pre>' in html


def test_html_emitter_text_with_and_without_mistune(monkeypatch):
    e = HtmlEmitter()

    import sys

    fake_mistune = SimpleNamespace(create_markdown=lambda escape=True: (lambda content: f"<em>{content}</em>"))
    monkeypatch.setitem(sys.modules, "mistune", fake_mistune)
    e.text("rich")

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mistune":
            raise ImportError("no mistune")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    e.text("para1\n\npara2")
    html = e.to_html()
    assert '<div class="narrative-text"><em>rich</em></div>' in html
    assert '<p class="narrative-text">para1</p>' in html
    assert '<p class="narrative-text">para2</p>' in html
