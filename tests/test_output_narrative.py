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
    assert HtmlEmitter._lang_for_tool("shell.execute") == "bash"

    e = HtmlEmitter()

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
    html = e.to_html()
    assert "<details class=\"tool-call tool-error\">" in html
    assert "diff-pair" in html and "tool-overflow" in html and "task-done" in html and "task-pending" in html

    pres2 = SimpleNamespace(headline="h", meta=None, removed=None, added=None, output=None, overflow=0, error=None, tasks=[])
    monkeypatch.setattr("siftd.output.tool_presenters.extract_tool_presentation", lambda *a, **k: pres2)
    e.tool_content("grep", 1, None, None, None)
    assert '<div class="tool-call">' in e.to_html()


def test_html_emitter_text_fallback_without_mistune(monkeypatch):
    e = HtmlEmitter()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mistune":
            raise ImportError("no mistune")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    e.text("para1\n\npara2")
    html = e.to_html()
    assert '<p class="narrative-text">para1</p>' in html
    assert '<p class="narrative-text">para2</p>' in html
