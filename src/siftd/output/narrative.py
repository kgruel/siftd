"""Presentation-layer emitters for narrative rendering.

The narrative walker, protocol, and JsonEmitter have moved to
siftd.serialization.narrative. This module re-exports them for
backward compatibility and provides presentation-specific emitters
(MarkdownEmitter, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-export from serialization layer for backward compatibility
from siftd.serialization.narrative import (
    JsonEmitter,
    NarrativeEmitter,
    walk_narrative,
)

if TYPE_CHECKING:
    pass

__all__ = ["JsonEmitter", "MarkdownEmitter", "NarrativeEmitter", "walk_narrative"]


class MarkdownEmitter:
    """Emits narrative as markdown lines.

    Accumulates into self.lines: list[str].
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def text(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self.lines.append(content)
        self.lines.append("")

    def thinking(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self.lines.append("> **Thinking**")
        self.lines.append(">")
        for line in content.split("\n"):
            self.lines.append(f"> {line}")
        self.lines.append("")

    def thinking_placeholder(self, *, event_id: str | None = None) -> None:
        del event_id
        self.lines.append("*[thinking]*")
        self.lines.append("")

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        parts = []
        for name, count, _status in tools:
            if count > 1:
                parts.append(f"{name} ×{count}")
            else:
                parts.append(name)
        self.lines.append(f"*[{', '.join(parts)}]*")
        self.lines.append("")

    def tool_content(
        self,
        name: str,
        count: int,
        raw_input: str | None,
        raw_result: str | None,
        status: str | None,
        *,
        event_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        del event_id, tool_call_id
        count_suffix = f" ×{count}" if count > 1 else ""
        status_suffix = f" ({status})" if status and status != "success" else ""
        header = f"- **{name}**{count_suffix}{status_suffix}"

        if raw_input:
            first_line = raw_input.strip().split("\n")[0]
            if len(first_line) > 100:
                first_line = first_line[:100] + "..."
            header += f" `{first_line}`"

        self.lines.append(header)

        if raw_result:
            result_text = raw_result.strip()
            if len(result_text) > 200:
                result_text = result_text[:200] + "..."
            for rline in result_text.split("\n"):
                self.lines.append(f"  {rline}")

    def tool_output(self, block_type: str, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self.lines.append(f"```\n{content}\n```")
        self.lines.append("")



class HtmlEmitter:
    """Emits narrative blocks as HTML fragments.

    Accumulates into self.parts: list[str].
    """

    def __init__(self) -> None:
        from html import escape

        self._escape = escape
        self.parts: list[str] = []

    def text(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        try:
            import mistune

            md = mistune.create_markdown(escape=True)
            rendered = md(content)
            self.parts.append(f'<div class="narrative-text">{rendered}</div>')
        except ImportError:
            # Fallback: escaped text when mistune not installed
            paragraphs = content.split("\n\n")
            for p in paragraphs:
                stripped = p.strip()
                if stripped:
                    self.parts.append(
                        f'<p class="narrative-text">{self._escape(stripped)}</p>'
                    )

    def thinking(self, content: str, *, event_id: str | None = None) -> None:
        del event_id
        self.parts.append(
            f'<details class="thinking" open>'
            f"<summary>Thinking</summary>"
            f"<pre>{self._escape(content)}</pre>"
            f"</details>"
        )

    def thinking_placeholder(self, *, event_id: str | None = None) -> None:
        del event_id
        self.parts.append('<span class="thinking placeholder">[thinking]</span>')

    def tool_summary(self, tools: list[tuple[str, int, str | None]]) -> None:
        items = []
        for tool_name, count, status in tools:
            label = self._escape(tool_name)
            if count > 1:
                label += f"&nbsp;&times;{count}"
            css = "tool-name"
            if status and status != "success":
                css += " tool-error"
            items.append(f'<span class="{css}">{label}</span>')
        self.parts.append(f'<div class="tool-summary">{" ".join(items)}</div>')

    @staticmethod
    def _lang_from_path(path: str) -> str:
        """Infer Prism language class from a file path."""
        ext_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".tsx": "tsx", ".jsx": "jsx", ".rs": "rust", ".go": "go",
            ".rb": "ruby", ".java": "java", ".sh": "bash", ".zsh": "bash",
            ".bash": "bash", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".md": "markdown", ".html": "markup",
            ".css": "css", ".sql": "sql", ".c": "c", ".cpp": "cpp",
            ".h": "c", ".hpp": "cpp", ".swift": "swift", ".kt": "kotlin",
            ".xml": "markup", ".lua": "lua", ".zig": "zig",
        }
        for ext, lang in ext_map.items():
            if path.endswith(ext):
                return lang
        return ""

    @staticmethod
    def _lang_for_tool(name: str) -> str:
        """Default language for a tool's output."""
        if name in ("shell.execute", "bash", "Bash"):
            return "bash"
        if name in ("search.grep", "grep", "Grep", "file.glob", "glob", "Glob"):
            return ""
        return ""

    def tool_content(
        self,
        name: str,
        count: int,
        raw_input: str | None,
        raw_result: str | None,
        status: str | None,
        *,
        event_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        del event_id, tool_call_id
        from siftd.output.tool_presenters import extract_tool_presentation

        e = self._escape
        pres = extract_tool_presentation(name, raw_input, raw_result, status)

        count_suffix = f" &times;{count}" if count > 1 else ""
        status_css = " tool-error" if status and status != "success" else ""

        # Infer language from tool name or file path in headline
        lang = self._lang_for_tool(name)
        if not lang and name in ("file.read", "read", "Read", "file.edit", "edit", "Edit",
                                  "file.write", "write", "Write"):
            lang = self._lang_from_path(pres.headline)
        lang_attr = f' class="language-{lang}"' if lang else ""

        # Summary line: tool name + headline (always visible)
        summary_meta = f' <span class="tool-meta">{e(pres.meta)}</span>' if pres.meta else ""
        has_content = (
            pres.removed is not None or pres.added is not None
            or pres.output or pres.error or pres.tasks
        )

        if has_content:
            parts = [f'<details class="tool-call{status_css}">']
            parts.append(
                f'<summary>'
                f'<span class="tool-name">{e(name)}{count_suffix}</span>'
                f' <code class="tool-headline">{e(pres.headline)}</code>'
                f'{summary_meta}</summary>'
            )
        else:
            parts = [f'<div class="tool-call{status_css}">']
            parts.append(f'<span class="tool-name">{e(name)}{count_suffix}</span>')
            parts.append(f' <code class="tool-headline">{e(pres.headline)}</code>')
            if summary_meta:
                parts.append(summary_meta)

        # Diff content (file.edit) — side-by-side when both present
        if pres.removed is not None and pres.added is not None:
            parts.append('<div class="diff-pair">')
            parts.append(f'<pre class="tool-diff tool-removed"><code{lang_attr}>{e(pres.removed)}</code></pre>')
            parts.append(f'<pre class="tool-diff tool-added"><code{lang_attr}>{e(pres.added)}</code></pre>')
            parts.append('</div>')
        elif pres.removed is not None:
            parts.append(f'<pre class="tool-diff tool-removed"><code{lang_attr}>{e(pres.removed)}</code></pre>')
        elif pres.added is not None:
            parts.append(f'<pre class="tool-diff tool-added"><code{lang_attr}>{e(pres.added)}</code></pre>')

        # Checklist (ui.todo)
        if pres.tasks:
            parts.append('<ul class="tool-tasks">')
            for text, done in pres.tasks:
                marker = "done" if done else "pending"
                parts.append(f'<li class="task-{marker}">{e(text)}</li>')
            parts.append("</ul>")

        # Output preview
        if pres.output:
            parts.append(f'<pre class="tool-result"><code{lang_attr}>{e(pres.output)}</code></pre>')
            if pres.overflow:
                parts.append(
                    f'<span class="tool-overflow">... +{pres.overflow} more lines</span>'
                )

        # Error
        if pres.error:
            parts.append(f'<pre class="tool-error">{e(pres.error)}</pre>')

        parts.append("</details>" if has_content else "</div>")
        self.parts.append("\n".join(parts))

    def tool_output(self, block_type: str, content: str, *, event_id: str | None = None) -> None:
        del block_type, event_id
        self.parts.append(
            f'<pre class="tool-result">{self._escape(content)}</pre>'
        )

    def to_html(self) -> str:
        return "\n".join(self.parts)

