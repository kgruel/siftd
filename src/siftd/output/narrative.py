"""Presentation-layer emitters for narrative rendering.

The narrative walker (``walk_narrative``), its protocol, and the machine emitters
(``JsonEmitter``, ``NarrativeEmitter``) live in ``siftd.serialization.narrative``.
This module owns the presentation-specific emitters — ``MarkdownEmitter`` and
``HtmlEmitter``.
"""

from __future__ import annotations

from siftd.output.common import truncate_text

__all__ = ["HtmlEmitter", "MarkdownEmitter"]


class MarkdownEmitter:
    """Emits narrative as markdown lines.

    Accumulates into self.lines: list[str].
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def text(
        self, content: str, *,
        event_id: str | None = None, block_id: str | None = None,
    ) -> None:
        del event_id, block_id
        self.lines.append(content)
        self.lines.append("")

    def thinking(
        self, content: str, *,
        event_id: str | None = None, block_id: str | None = None,
    ) -> None:
        del event_id, block_id
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
            first_line = truncate_text(raw_input.strip().split("\n")[0], 100)
            header += f" `{first_line}`"

        self.lines.append(header)

        if raw_result:
            result_text = truncate_text(raw_result.strip(), 200)
            for rline in result_text.split("\n"):
                self.lines.append(f"  {rline}")

    def tool_output(
        self, block_type: str, content: str, *,
        event_id: str | None = None, block_id: str | None = None,
    ) -> None:
        del event_id, block_id
        self.lines.append(f"```\n{content}\n```")
        self.lines.append("")



class HtmlEmitter:
    """Emits narrative blocks as HTML fragments.

    Accumulates into self.parts: list[str].

    Each emitted element carries a ``data-event-id`` anchor on the *first*
    block of every new event run (a response = many blocks under one event_id,
    so anchoring once per run keeps the anchors — and the eventual element
    ids/tags — unique). This is the response-precise scroll/highlight target the
    search → "open in folio" jump lands on; ``target_event_id`` marks that one
    landed element ``is-target``. ``event_id`` values are events-table ULIDs
    (``[0-9A-Z]{26}`` — selector- and attribute-safe), still HTML-escaped here.
    """

    def __init__(
        self,
        target_event_id: str | None = None,
        *,
        tool_seq: list[dict] | None = None,
        turn_no: int | None = None,
        event_tags: dict[str, list[tuple[str, str]]] | None = None,
        block_tags: dict[str, list[tuple[str, str]]] | None = None,
        interactive_tags: bool = False,
        tag_action_url: str = "",
        tag_suggest_url: str = "",
        copy_url_base: str = "",
    ) -> None:
        from html import escape

        self._escape = escape
        self.parts: list[str] = []
        self._target = target_event_id
        self._last_event_id: str | None = None
        # Whether a per-run wrapper div is currently open (closed on the next run
        # transition or at to_html finalize). Only used on the interactive path.
        self._run_open = False
        # Trace-mode element tagging (WS4b): the batch (name, kind)-pair map, plus
        # the interactivity + route context, let each tool-call block carry a
        # top-right hover-reveal tag affordance. Off by default — the CLI html
        # export and the search-context slice pass nothing and stay chip-free.
        # block_tags is the separate event_content-keyed map — block ids and
        # event ids are distinct keyspaces (the WS8 chip-leak lesson), so the
        # affordance picks its map by entity kind, never by key coincidence.
        self._event_tags = event_tags
        self._block_tags = block_tags
        self._interactive_tags = interactive_tags
        self._tag_action_url = tag_action_url
        self._tag_suggest_url = tag_suggest_url
        # Mount point of the raw-text route (serve knowledge, threaded like
        # tag_action_url). Empty → no Copy buttons (CLI html export has no
        # server to fetch from).
        self._copy_url_base = copy_url_base
        # Optional Activity registry (the folio's chronological tool ledger).
        # When passed, each tool-call gets a folio-unique ``id="evt-N"`` anchor
        # and one record {id, name, target, status, turn} is appended — the
        # aside renders from it and enhance.js scroll-spy keys off the ids.
        # Shared across an entire folio body so N is globally sequential; the
        # per-turn caller passes the same list plus the current ``turn_no``.
        self._tool_seq = tool_seq
        self._turn_no = turn_no

    def _register_tool(self, name: str, target: str, status: str | None) -> str:
        """Record one tool call in the Activity registry; return its id attr."""
        if self._tool_seq is None:
            return ""
        k = len(self._tool_seq) + 1
        anchor = f"evt-{k}"
        self._tool_seq.append({
            "id": anchor,
            "name": name,
            "target": target,
            "status": status,
            "turn": self._turn_no,
        })
        return f' id="{anchor}"'

    def _anchor(self, event_id: str | None) -> tuple[str, str]:
        """Return ``(attrs, extra_class)`` for the first element of a new event
        run — the anchor the folio jump scrolls/highlights to. Emitted once per
        ``event_id`` run; an empty/repeat id yields no anchor (so a multi-block
        response gets exactly one ``data-event-id``).

        Uniqueness relies on an upstream invariant: ``_build_narrative`` emits all
        blocks of one response contiguously under that response's id, and each
        assistant turn gets a fresh emitter — so an id never recurs after another
        intervenes, and a single ``_last_event_id`` suffices to dedupe a run.

        A run transition is also where the per-run response tag affordance is
        emitted (WS4b slice 2): the whole run is wrapped in a ``.trace-block--run``
        container carrying a top-right menu that tags the response event, so trace
        mode tags every block from its own corner — the run for prose/thinking, an
        inner ``.trace-block--tool`` for each tool call. Thinking has no separate
        affordance: it tags its owning response, which the run menu already is."""
        if not event_id or event_id == self._last_event_id:
            return "", ""
        self._last_event_id = event_id
        # Open a positioned wrapper per response run so the run-level menu can pin
        # to its corner. Only when tagging is interactive — otherwise the output
        # (CLI html export, search slice) stays byte-for-byte as before.
        if self._interactive_tags:
            if self._run_open:
                self.parts.append("</div>")
            affordance = self._tag_affordance("response", event_id)
            self.parts.append(f'<div class="trace-block trace-block--run">{affordance}')
            self._run_open = True
        attrs = f' data-event-id="{self._escape(event_id)}"'
        cls = " is-target" if event_id == self._target else ""
        return attrs, cls

    def _tag_affordance(
        self, entity_type: str, target_id: str | None, *, copy_controls: str = "",
    ) -> str:
        """Top-right hover-reveal tag menu for one taggable trace block, or "".

        Rendered only when interactive tagging is on and the block has a real
        target id. The menu is a native ``<details>`` (the ``+`` is its summary)
        so the dropdown opens and stays open on click with no JS — the CSP's
        missing ``unsafe-eval`` never comes up. Its panel body is the same tag
        section reading mode uses (chips with × + add-input), so a mutation swaps
        just the inner section and the open dropdown persists — ``copy_controls``
        (see :meth:`_copy_button`) sit above it, outside the swap. The affordance
        is a SIBLING of the block's ``<details>`` (never a child): a collapsed
        block hides its non-summary children, so a child affordance would vanish
        when the tool call is folded — and a sibling's clicks never toggle the
        block.
        """
        if not self._interactive_tags or not target_id:
            return ""
        from siftd.output.html_fmt import _render_tag_section

        # Pick the map by entity kind: block targets are event_content ULIDs,
        # everything else is an events ULID. Never fall through across maps.
        tag_map = self._block_tags if entity_type == "block" else self._event_tags
        pairs = (tag_map or {}).get(target_id, [])
        section = _render_tag_section(
            target_id, pairs, True,
            tag_action_url=self._tag_action_url,
            tag_suggest_url=self._tag_suggest_url,
            entity_type=entity_type,
            section_class="tag-section tag-section--elem",
        )
        return (
            '<details class="tag-menu">'
            f'<summary class="tag-menu__toggle" title="Tag this {self._escape(entity_type)}"'
            ' aria-label="Tag">+</summary>'
            f'<div class="tag-menu__panel">{copy_controls}{section}</div>'
            "</details>"
        )

    def _copy_button(self, kind: str, target_id: str | None, label: str) -> str:
        """One copy control for the panel, or "": fetches the stored payload
        verbatim from the raw-text route (``{base}/{kind}/{id}``) and writes it
        to the clipboard — the rendered DOM is not a faithful copy source
        (markdown re-rendering, presenter line caps). enhance.js owns the
        click via ``data-copy-src``. ``kind`` is api's COPY_TEXT_KINDS word."""
        if not self._copy_url_base or not target_id:
            return ""
        return (
            f'<button type="button" class="tag-menu__copy"'
            f' data-copy-src="{self._copy_url_base}/{kind}/{self._escape(target_id)}"'
            f">{self._escape(label)}</button>"
        )

    def _wrap_block(self, block_html: str, block_id: str | None) -> str:
        """Wrap one prose/thinking/tool-output element in the per-block action
        surface — a positioned container carrying ``data-block-id`` plus the
        corner tag menu — or return it unwrapped.

        Same sibling-wrapper rule as the tool affordance: a collapsed
        ``<details>`` block hides non-summary children, so the menu must live
        beside the block, never inside it. Emitted only on the interactive
        path with a real block id — CLI html export and the search-context
        slice stay byte-for-byte unchanged.
        """
        affordance = self._tag_affordance(
            "block", block_id,
            copy_controls=self._copy_button("block", block_id, "Copy text"),
        )
        if not affordance:
            return block_html
        return (
            f'<div class="trace-block trace-block--blk"'
            f' data-block-id="{self._escape(block_id or "")}">'
            f"{affordance}\n{block_html}</div>"
        )

    def text(
        self, content: str, *,
        event_id: str | None = None, block_id: str | None = None,
    ) -> None:
        attrs, cls = self._anchor(event_id)
        try:
            import mistune

            md = mistune.create_markdown(escape=True)
            rendered = md(content)
            self.parts.append(self._wrap_block(
                f'<div class="narrative-text{cls}"{attrs}>{rendered}</div>', block_id,
            ))
        except ImportError:
            # Fallback: escaped text when mistune not installed. Only the first
            # paragraph carries the anchor (one per event run).
            paras: list[str] = []
            first = True
            for p in content.split("\n\n"):
                stripped = p.strip()
                if not stripped:
                    continue
                a, c = (attrs, cls) if first else ("", "")
                paras.append(
                    f'<p class="narrative-text{c}"{a}>{self._escape(stripped)}</p>'
                )
                first = False
            if not paras:
                return
            if self._interactive_tags and block_id:
                self.parts.append(self._wrap_block("\n".join(paras), block_id))
            else:
                # Unwrapped: keep each paragraph a separate part (byte-identical
                # to the pre-block output, which joins parts with newlines).
                self.parts.extend(paras)

    def thinking(
        self, content: str, *,
        event_id: str | None = None, block_id: str | None = None,
    ) -> None:
        attrs, cls = self._anchor(event_id)
        # Collapsed by default, like the inline tool calls: the trace shows the
        # agent's flow as a skim, with thinking/tool payloads expanded on demand
        # rather than dumped as a wall.
        self.parts.append(self._wrap_block(
            f'<details class="thinking{cls}"{attrs}>'
            f"<summary>Thinking</summary>"
            f"<pre>{self._escape(content)}</pre>"
            f"</details>",
            block_id,
        ))

    def thinking_placeholder(self, *, event_id: str | None = None) -> None:
        attrs, cls = self._anchor(event_id)
        self.parts.append(
            f'<span class="thinking placeholder{cls}"{attrs}>[thinking]</span>'
        )

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
        anchor_attrs, anchor_cls = self._anchor(event_id)
        from siftd.output.tool_presenters import extract_tool_presentation

        e = self._escape
        # tool_chars=0 → the FULL result (no 120-char preview cut). This emitter
        # is the trace/detail view: the result lives behind a collapsed
        # <details>, so expanding it should show everything, not a stub.
        pres = extract_tool_presentation(name, raw_input, raw_result, status, tool_chars=0)

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

        # Activity-registry anchor: a folio-unique id the chronological aside
        # links to (and the scroll-spy mirrors). Target line = headline + meta.
        seq_target = pres.headline + (f" {pres.meta}" if pres.meta else "")
        id_attr = self._register_tool(name, seq_target, status)

        if has_content:
            parts = [f'<details class="tool-call{status_css}{anchor_cls}"{id_attr}{anchor_attrs}>']
            parts.append(
                f'<summary>'
                f'<span class="tool-name">{e(name)}{count_suffix}</span>'
                f' <code class="tool-headline">{e(pres.headline)}</code>'
                f'{summary_meta}</summary>'
            )
        else:
            parts = [f'<div class="tool-call{status_css}{anchor_cls}"{id_attr}{anchor_attrs}>']
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

        # A tagged run of identical tools collapses to one row with no single id
        # (tool_call_id is None then) — no affordance on an aggregate. Otherwise
        # wrap the block in a positioned container carrying the top-right tag
        # menu. Tool panels get copy controls for the payloads the panel shows:
        # input and result copy from the STORE (presenters cap lines in the DOM),
        # each offered only when that payload is actually present.
        affordance = self._tag_affordance(
            "tool_call", tool_call_id,
            copy_controls=(
                (self._copy_button("tool_input", tool_call_id, "Copy input") if raw_input else "")
                + (self._copy_button("tool_result", tool_call_id, "Copy result") if raw_result else "")
            ),
        )
        if affordance:
            block = "\n".join(parts)
            self.parts.append(
                f'<div class="trace-block trace-block--tool">{affordance}\n{block}</div>'
            )
        else:
            self.parts.append("\n".join(parts))

    def tool_output(
        self, block_type: str, content: str, *,
        event_id: str | None = None, block_id: str | None = None,
    ) -> None:
        del block_type
        attrs, cls = self._anchor(event_id)
        self.parts.append(self._wrap_block(
            f'<pre class="tool-result{cls}"{attrs}>{self._escape(content)}</pre>',
            block_id,
        ))

    def to_html(self) -> str:
        # Close the final run wrapper if one is still open (idempotent — a second
        # call finds no open run).
        if self._run_open:
            self.parts.append("</div>")
            self._run_open = False
        return "\n".join(self.parts)

