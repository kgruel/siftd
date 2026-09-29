"""Real-browser CSP smoke (T3 of docs/guides/serve-browser-testing.md).

Run via ``./dev browser-smoke``. Its assertions are shared with pytest coverage;
the executable is an exit-code program: 0 = all checks pass, 1 = check failed, 2 = harness fault
(broken detector, no chromium, server never came up).

The only tier that catches CSP violations from vendored-library *internals*
(htmx's ``new Function`` lives inside htmx.min.js; Prism's autoloader injects
``<script>`` elements at runtime) and real-input behavior (htmx triggers,
inline handlers) — none of which TestClient or the T1/T2 static tiers can see.

Method (each rule exists because its violation produced a false PASS):
- headless Chromium over raw CDP locally; native Playwright page CDP remotely
- violations detected TWO ways: in-page ``securitypolicyviolation`` listener
  + the CDP security-source log
- POSITIVE CONTROL FIRST: an off-origin <script src> must be blocked and
  detected, else the instrument is broken and every negative is meaningless
- all interactions via Input.dispatchMouseEvent / dispatchKeyEvent — real
  browser input runs handlers in the genuine page world; Runtime.evaluate
  (and element.click() called from it) executes CSP-EXEMPT and falsely passes
- the server is the from-source venv ``siftd`` entrypoint, never the PATH
  binary (a uv-tool snapshot that drifts)
- no fixed sleeps: every step waits on a named condition with a deadline
  (the navigation's ``Page.loadEventFired``, an htmx swap settled since the
  action plus the DOM it produced, or the handler's effect). A fixed settle is
  both slow and a guess; a missed condition fails naming what never happened.
  Events keep being collected while a wait polls.

Layout note: the CDP driver, lifecycle, and verdict are shell-agnostic; only
``flow()`` knows the current two-pane shell's selectors. When the Swiss shell
lands, rewrite ``flow()`` (see the swiss variant preserved in project memory)
and leave the rest alone.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

import httpx

PORT = int(os.environ.get("SIFTD_SMOKE_PORT", "8378"))
CDP_PORT = int(os.environ.get("SIFTD_SMOKE_CDP_PORT", "9378"))
BASE = f"http://127.0.0.1:{PORT}"

HEADER_PATHS = ["/", "/static/vendor/htmx.min.js", "/query", "/api/v1/health"]
EXPECT_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def _find_chromium() -> str | None:
    if env_bin := os.environ.get("CHROMIUM_BIN"):
        if shutil.which(env_bin):
            return env_bin
        print(f"FATAL: CHROMIUM_BIN={env_bin} is not an executable")
        return None
    for candidate in ("chromium", "chromium-browser"):
        if found := shutil.which(candidate):
            return found
    for path in ("/opt/homebrew/bin/chromium", "/usr/bin/chromium"):
        if Path(path).exists():
            return path
    return None


# ---------------------------------------------------------------------------
# Fixture: 3 conversations, one with a rust code fence (exercises the Prism
# autoloader's runtime <script> injection — the vendored-internals case).
# ---------------------------------------------------------------------------

RUST = (
    "Here is the fix, with a fenced block in a non-core Prism language:\n\n"
    "```rust\nfn main() {\n    let answer: u32 = 42;\n    println!(\"answer={}\", answer);\n}\n```\n\n"
    "And inline prose after the code."
)


def build_fixture(db_path: Path) -> str:
    """Create the fixture DB; return the conversation id with the code fence."""
    from siftd.api.search import rebuild_fts_index
    from siftd.storage.sqlite import (
        create_database,
        get_or_create_harness,
        get_or_create_model,
        get_or_create_provider,
        get_or_create_workspace,
        insert_conversation,
        insert_prompt,
        insert_prompt_content,
        insert_response,
        insert_response_content,
        insert_tool_call,
    )
    from siftd.storage.usage_rollup import rebuild_rollups

    conn = create_database(db_path)
    h = get_or_create_harness(conn, "csp-smoke", source="smoke", log_format="jsonl")
    w = get_or_create_workspace(conn, "/work/csp-smoke", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-3-5-sonnet")
    p = get_or_create_provider(conn, "anthropic")

    ids = []
    for ci in range(3):
        cid = insert_conversation(
            conn, external_id=f"csp-{ci}", harness_id=h, workspace_id=w,
            started_at=f"2024-02-0{ci + 1}T10:00:00Z",
        )
        ids.append(cid)
        for ti in range(3):
            ts = f"2024-02-0{ci + 1}T10:0{ti}:00Z"
            pid = insert_prompt(conn, cid, f"p-{ci}-{ti}", ts)
            insert_prompt_content(
                conn, pid, 0, "text",
                json.dumps({"text": f"csp-smoke prompt conv={ci} turn={ti} anchor-find-needle"}),
            )
            rid = insert_response(
                conn, cid, pid, m, p, f"r-{ci}-{ti}", ts,
                input_tokens=100 + ci, output_tokens=50 + ti,
            )
            body = RUST if (ci == 2 and ti == 1) else f"plain response conv={ci} turn={ti}"
            insert_response_content(conn, rid, 0, "text", json.dumps({"text": body}))
            if ci == 2 and ti == 0:
                # A tool call in the code_conv so the folio's trace mode has
                # interleaved I/O to inline (the reading→trace toggle smoke).
                insert_response_content(
                    conn, rid, 1, "tool_use",
                    json.dumps({"id": "toolu_smoke", "name": "Read",
                                "input": {"file_path": "x.py"}}),
                )
                insert_tool_call(
                    conn, rid, cid, None, "toolu_smoke",
                    json.dumps({"file_path": "x.py"}),
                    json.dumps({"text": "file body"}), "success", ts,
                )

    # A sub-agent of the newest root (csp-2), to exercise Sessions nesting:
    # collapsed-by-default + chevron expand. external_id is "<root>::agent::<id>".
    sub = insert_conversation(
        conn, external_id="csp-2::agent::sub1", harness_id=h, workspace_id=w,
        started_at="2024-02-03T10:05:00Z",
    )
    spid = insert_prompt(conn, sub, "p-sub", "2024-02-03T10:05:00Z")
    insert_prompt_content(
        conn, spid, 0, "text",
        json.dumps({"text": "sub-agent prompt anchor-find-needle"}),
    )
    srid = insert_response(
        conn, sub, spid, m, p, "r-sub", "2024-02-03T10:05:01Z",
        input_tokens=10, output_tokens=5,
    )
    insert_response_content(conn, srid, 0, "text", json.dumps({"text": "sub-agent response"}))

    # A second workspace so the Workspaces view lists >1 row — the body filter
    # check needs something to hide while keeping a match shown.
    w2 = get_or_create_workspace(conn, "/work/other-proj", "2024-01-01T00:00:00Z")
    cid2 = insert_conversation(
        conn, external_id="other-0", harness_id=h, workspace_id=w2,
        started_at="2024-02-05T10:00:00Z",
    )
    pid2 = insert_prompt(conn, cid2, "p-other", "2024-02-05T10:00:00Z")
    insert_prompt_content(
        conn, pid2, 0, "text",
        json.dumps({"text": "other-proj prompt anchor-find-needle"}),
    )
    rid2 = insert_response(
        conn, cid2, pid2, m, p, "r-other", "2024-02-05T10:00:01Z",
        input_tokens=20, output_tokens=10,
    )
    insert_response_content(conn, rid2, 0, "text", json.dumps({"text": "other-proj response"}))

    rebuild_fts_index(conn)
    # The reckoning + workspace cadence read usage_by_conv_model (a real ingest
    # builds it); build it here so the Stats charts have data to scale.
    rebuild_rollups(conn)
    conn.commit()
    conn.close()
    return ids[2]


# ---------------------------------------------------------------------------
# CDP driver (shell-agnostic)
# ---------------------------------------------------------------------------

# Deadlines, not delays: every wait returns as soon as its condition holds and
# these only bound how long a missing condition may take to become a failure.
# Generous on purpose: a remote browser adds a network round trip per command.
NAVIGATION_TIMEOUT = 20.0
WAIT_TIMEOUT = 10.0
POLL_INTERVAL = 0.1

# No htmx request in flight: htmx marks the requesting element (or its
# indicator) with .htmx-request for the request's lifetime.
HTMX_IDLE = "!document.querySelector('.htmx-request')"


class ReadinessTimeout(RuntimeError):
    """A named readiness condition did not hold before its deadline."""

    def __init__(self, condition: str, timeout: float, last) -> None:
        self.condition = condition
        super().__init__(f"timed out after {timeout:g}s waiting for: {condition} (last: {last!r})")


class CDP:
    """Minimal CDP client over any wire with ``send(str)`` / ``recv() -> str``.

    Every message that is not the reply being awaited is appended to
    ``events``, whichever method is reading the wire — so the security log and
    exceptions are collected continuously, including while a wait polls.
    """

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.events = []

    async def cmd(self, method, params=None):
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    async def drain(self, seconds):
        """Collect events for up to ``seconds``: poll spacing, never synchronisation."""
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=left))
                self.events.append(msg)
            except TimeoutError:
                return

    async def eval(self, expr):
        r = await self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    async def wait_for_event(self, method, *, since, condition, timeout=NAVIGATION_TIMEOUT):
        """Return the first ``method`` event at or after ``events[since]``.

        Scanning from ``since`` (taken before the triggering command was sent)
        catches an event that arrived ahead of that command's reply.
        """
        end = time.monotonic() + timeout
        index = since
        while True:
            while index < len(self.events):
                event = self.events[index]
                index += 1
                if event.get("method") == method:
                    return event
            left = end - time.monotonic()
            if left <= 0:
                raise ReadinessTimeout(condition, timeout, f"no {method} event")
            await self.drain(min(POLL_INTERVAL, left))

    async def wait_until(self, expr, condition, timeout=WAIT_TIMEOUT):
        """Poll a page expression until truthy; return its value or raise ReadinessTimeout.

        An evaluation error (e.g. the execution context is being replaced by a
        navigation) counts as "not yet". Between polls the wire is drained, so
        events keep being collected.
        """
        end = time.monotonic() + timeout
        last = None
        while True:
            try:
                last = await self.eval(expr)
            except RuntimeError as error:
                last = f"evaluation error: {error}"
            else:
                if last:
                    return last
            left = end - time.monotonic()
            if left <= 0:
                raise ReadinessTimeout(condition, timeout, last)
            await self.drain(min(POLL_INTERVAL, left))

    async def await_outcome(self, expr, timeout=WAIT_TIMEOUT):
        """Wait for an asserted outcome; return its last value, truthy or not.

        For a synchronous handler's effect that is itself the checked result:
        a miss is the caller's check to report, not a reason to stop the flow.
        """
        try:
            return await self.wait_until(expr, "asserted outcome", timeout)
        except ReadinessTimeout:
            return await self.eval(expr)

    async def settle_mark(self):
        """Count of htmx settles so far; pass to ``wait_for_swap`` after acting."""
        return await self.eval("window.__settled || 0")

    async def wait_for_swap(self, mark, ready, condition, timeout=WAIT_TIMEOUT):
        """Wait for an htmx swap settled since ``mark``, no request in flight, and ``ready``.

        The settle counter's listener sits on ``document``, so it runs after
        enhance.js's ``htmx:afterSettle`` handler on ``body``: a counted settle
        means the page's JS enhancement has already run on the new content.
        """
        return await self.wait_until(
            f"(window.__settled || 0) > {int(mark)} && {HTMX_IDLE} && ({ready})",
            condition,
            timeout,
        )

    async def click(self, selector):
        """Real browser-level click at the element's center (NOT element.click())."""
        box = await self.eval(
            f"(function(){{var el=document.querySelector({json.dumps(selector)});"
            f"if(!el) return null; el.scrollIntoView({{block:'center'}});"
            f"var r=el.getBoundingClientRect();"
            f"return [r.left+r.width/2, r.top+r.height/2];}})()"
        )
        if not box:
            raise RuntimeError(f"selector not found: {selector}")
        x, y = box
        for t in ("mousePressed", "mouseReleased"):
            await self.cmd("Input.dispatchMouseEvent", {
                "type": t, "x": x, "y": y, "button": "left", "clickCount": 1,
            })

    async def type_text(self, text):
        """Real key events (keyDown w/ text + keyUp) so htmx keyup triggers fire."""
        for ch in text:
            await self.cmd("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "key": ch})
            await self.cmd("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})

    def security_log_entries(self):
        out = []
        for e in self.events:
            if e.get("method") == "Log.entryAdded":
                entry = e["params"]["entry"]
                if entry.get("source") == "security":
                    out.append(entry.get("text", ""))
            elif e.get("method") == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append("EXC: " + (d.get("exception", {}).get("description") or d.get("text", ""))[:200])
        return out


LISTENER = """
window.__v = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__v.push({dir: e.violatedDirective, blocked: e.blockedURI, src: e.sourceFile, line: e.lineNumber});
});
window.__settled = 0;
document.addEventListener('htmx:afterSettle', function () { window.__settled++; });
"""


async def navigate(cdp, url, ready=None, condition=None, timeout=NAVIGATION_TIMEOUT):
    """Load ``url`` as a new document, then optionally wait for ``ready``.

    Waits for this navigation's ``Page.loadEventFired`` (Page domain enabled by
    ``run_assertions``). Load is not readiness for this UI — #main mounts its
    view with an htmx request after load — so callers pass the page condition
    their next step depends on.
    """
    since = len(cdp.events)
    result = await cdp.cmd("Page.navigate", {"url": url})
    if result.get("errorText"):
        raise RuntimeError(f"fixture navigation failed: {result['errorText']}")
    await cdp.wait_for_event("Page.loadEventFired", since=since, condition=f"load event for {url}", timeout=timeout)
    if ready is not None:
        await cdp.wait_until(ready, condition or f"{url} ready", timeout)


# ---------------------------------------------------------------------------
# Flow: the Swiss shell. Shell-SPECIFIC — rewrite when the UI does.
# ---------------------------------------------------------------------------


def _main_view_is(view):
    """#main's mounted view root (every view fragment carries data-view) is ``view``."""
    return (
        "(function(){var v=document.querySelector('#main [data-view]');"
        f"return !!v && v.getAttribute('data-view')==={json.dumps(view)};}})()"
    )


# Shell loaded and #main's load-triggered view mount has settled.
SHELL_READY = (
    "!!document.querySelector('.sw-rail') && !!window.htmx"
    f" && !!document.querySelector('#main [data-view]') && {HTMX_IDLE}"
)


async def flow(cdp, check, code_conv):
    # Synchronisation rule — no fixed sleeps. Every action is followed by a wait
    # on a concrete condition, with a deadline:
    # - a readiness gate (navigate / wait_until / wait_for_swap) where a later
    #   step depends on it: a navigation's load, an htmx swap settled since the
    #   action plus the DOM it produced, or a history restore's state. A miss
    #   raises ReadinessTimeout naming the condition and stops the flow;
    # - an outcome wait (await_outcome) where the condition is itself the
    #   asserted result of a synchronous handler and nothing later depends on
    #   it: a miss returns the falsy value and the check reports it.
    await navigate(cdp, f"{BASE}/", SHELL_READY, "shell loaded and #main view mounted")
    shell_ok = await cdp.eval("!!document.querySelector('.sw-rail') && !!window.htmx")
    check("swiss shell rendered + vendored htmx loaded", bool(shell_ok))

    # rail nav: every view (stubs included) must swap #main via htmx
    for view in ("sessions", "search", "transcript", "tags", "workspaces", "stats"):
        mark = await cdp.settle_mark()
        await cdp.click(f'a[data-view="{view}"]')
        await cdp.wait_for_swap(mark, _main_view_is(view), f"rail nav to {view} swapped #main")
        children = await cdp.eval(
            "document.getElementById('main') ? document.getElementById('main').childElementCount : -1"
        )
        check(
            f"rail nav swaps #main: {view}",
            children is not None and children > 0,
            f"children={children}",
        )

    # URL-as-state: rail nav pushes canonical /?view= URLs, and browser
    # back/forward restores the prior view (htmx history). Drive real popstate.
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="tags"]')
    await cdp.wait_for_swap(mark, _main_view_is("tags"), "rail nav to tags swapped #main")
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="stats"]')
    await cdp.wait_for_swap(mark, _main_view_is("stats"), "rail nav to stats swapped #main")
    at_stats = await cdp.eval("location.search")
    check("rail nav pushes canonical ?view= URL", at_stats == "?view=stats", f"search={at_stats}")
    await cdp.eval("history.back()")
    # A history restore is not an htmx swap (no settle to count): wait on the
    # restored state itself.
    await cdp.wait_until(
        f"location.search === '?view=tags' && {_main_view_is('tags')}",
        "history back restored ?view=tags into #main",
    )
    back_url = await cdp.eval("location.search")
    back_view = await cdp.eval(
        "(function(){var v=document.querySelector('#main [data-view]');"
        "return v ? v.getAttribute('data-view') : null;})()"
    )
    check(
        "browser back restores the prior view (URL + #main)",
        back_url == "?view=tags" and back_view == "tags",
        f"url={back_url} view={back_view}",
    )

    # find box: real keystrokes -> htmx keyup trigger (350ms delay)
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="search"]')
    await cdp.wait_for_swap(
        mark,
        "!!document.querySelector('#main .find') && !!document.querySelector('#list > *')"
        " && !!document.querySelector('#filters input[name=\"search\"]')",
        "search view mounted its #filters strip and #list",
    )
    # Slice 2b: bare Find (no term, no facet) opens as the search PROMPT, not a
    # recency list.
    prompt_shown = await cdp.eval(
        "!!document.querySelector('#list .find-prompt') "
        "&& !document.querySelector('#list .conversation-list')"
    )
    check("bare Find opens as the search prompt (no recency list)", prompt_shown is True)
    mark = await cdp.settle_mark()
    await cdp.click('input[name="search"]')
    await cdp.type_text("needle")
    await cdp.wait_for_swap(
        mark,
        "location.search.includes('q=needle') && !!document.querySelector('#list .search-results')",
        "find keystrokes for 'needle' swapped results into #list",
    )
    # A content query now runs the real engine: ranked excerpt hits, not the
    # recency list table. Each hit is a .search-hit article.
    hits = await cdp.eval(
        "document.querySelectorAll('#list .search-results .search-hit').length"
    )
    check("find keystrokes run engine search", hits is not None and hits > 0, f"hits={hits}")
    engine_named = await cdp.eval(
        "/\\[(fts|hybrid|semantic)\\]/.test(document.getElementById('list').textContent)"
    )
    check("find search names the engine that ran", bool(engine_named))
    # editorial hit markup: a hanging meta gutter + a score meter that
    # drawHitMeters scales under CSP (--w set from data-n). Guards the new JS.
    meter_drawn = await cdp.eval(
        "(function(){var m=document.querySelector('#list .search-hit .hit-meta .hit-meter');"
        "return !!m && !!m.style.getPropertyValue('--w');})()"
    )
    check("search hit-meta gutter + score meter drawn under CSP", meter_drawn is True)

    # Slice 2c: with results showing, the strip COLLAPSES (CSS :has) — the
    # secondary "more filters" disclosure is hidden and the expand chevron shows.
    more_shown = "getComputedStyle(document.querySelector('#filters .find__more')).display!=='none'"
    collapsed = await cdp.eval(
        "(function(){var more=document.querySelector('#filters .find__more');"
        "var ex=document.querySelector('#filters .find__expand');"
        "return getComputedStyle(more).display==='none'"
        " && getComputedStyle(ex).display!=='none';})()"
    )
    check("control strip collapses to a refinement bar on search", collapsed is True)
    # Clicking the chevron force-expands back to the builder (more filters shown).
    # A label toggling a checkbox that CSS reads: no request, the style is the state.
    # (A plain outcome wait where a miss is the check's to report; a readiness
    # gate — wait_until / wait_for_swap — where the next step depends on it.)
    await cdp.click('#filters .find__expand')
    reexpanded = await cdp.await_outcome(more_shown)
    check("expand chevron re-opens the builder", reexpanded is True)
    # Collapse again so the view-toggle check below runs against the bar state.
    await cdp.click('#filters .find__expand')
    await cdp.wait_until(f"!({more_shown})", "expand chevron collapses the builder again")

    # view toggle: changing the result-shape <select> re-runs the query through
    # the same recipe server-side and swaps #list to the thread shape. Guards
    # the htmx wiring — the toggle must include into #filters and fire on change
    # — which the unit tests (firing /query?view= directly) can't see. Must
    # reset to chunks after: the thread view's tier1 .search-hit.expanded has no
    # detail link, so the downstream row-click test needs the clickable chunks
    # view back.
    mark = await cdp.settle_mark()
    await cdp.eval(
        "(function(){var s=document.querySelector('select[name=\"view\"]');"
        "if(s){s.value='thread';s.dispatchEvent(new Event('change'));}})()"
    )
    await cdp.wait_for_swap(
        mark, "!!document.querySelector('#list .search-results.thread')",
        "view toggle swapped #list to the thread shape",
    )
    thread_shape = await cdp.eval(
        "!!document.querySelector('#list .search-results.thread')"
    )
    check("view toggle swaps result shape to thread", bool(thread_shape), f"ok={thread_shape}")
    mark = await cdp.settle_mark()
    await cdp.eval(
        "(function(){var s=document.querySelector('select[name=\"view\"]');"
        "if(s){s.value='chunks';s.dispatchEvent(new Event('change'));}})()"
    )
    await cdp.wait_for_swap(
        mark, "!!document.querySelector('#list .search-results.chunks')",
        "view toggle swapped #list back to the chunks shape",
    )

    # context unfold: clicking a hit's unfold control expands the surrounding
    # exchanges IN PLACE (the #list result list stays — no navigation), proving
    # the windowed read + that the control (a sibling of the folio-navigable
    # block) does not bubble to the folio jump. Then unfold a second, still-
    # collapsed hit and confirm the first stays open (independent per-hit state).
    mark = await cdp.settle_mark()
    await cdp.click("#list .search-hit .hit-unfold")
    await cdp.wait_for_swap(
        mark, "document.querySelectorAll('#list .hit-context__slice').length >= 1",
        "hit unfold swapped a context slice into the hit",
    )
    unfolded = await cdp.eval(
        "document.querySelectorAll('#list .hit-context__slice .turn').length"
    )
    still_list = await cdp.eval("!!document.querySelector('#list .search-results.chunks')")
    no_nav = await cdp.eval("!document.querySelector('#main .folio')")
    check("hit unfold expands context in place", unfolded is not None and unfolded > 0,
          f"turns={unfolded}")
    check("unfold does not navigate to the folio", bool(still_list) and bool(no_nav))
    # Unfold a different, still-collapsed hit (one whose control still reads
    # "unfold context"); the first must remain expanded.
    await cdp.eval(
        "(function(){var hits=document.querySelectorAll('#list .search-hit');"
        "for(var i=0;i<hits.length;i++){var b=hits[i].querySelector('.hit-unfold');"
        "if(b&&b.textContent.indexOf('unfold context')>=0){b.click();return true;}}"
        "return false;})()"
    )
    await cdp.await_outcome("document.querySelectorAll('#list .hit-context__slice').length >= 2")
    slices = await cdp.eval("document.querySelectorAll('#list .hit-context__slice').length")
    check("multiple hits unfold independently", slices is not None and slices >= 2,
          f"slices={slices}")

    # FTS5 footgun characters must not error the list pane. The search swap
    # settling is the readiness signal (syncFindUrl mirrors the settled box into
    # the URL); the check is on what the swap rendered.
    footgun = 'a"(:*'
    await cdp.eval("document.querySelector('input[name=\"search\"]').value=''")
    mark = await cdp.settle_mark()
    await cdp.click('input[name="search"]')
    await cdp.type_text(footgun)
    await cdp.wait_for_swap(
        mark,
        f"location.search.includes('q=' + encodeURIComponent({json.dumps(footgun)}))",
        "find search for the FTS5 punctuation input settled",
    )
    err = await cdp.eval(
        "document.getElementById('list').textContent.toLowerCase().includes('error')"
    )
    check("FTS5 punctuation input survives", not err)

    # row click: a Find list row must mount the folio into #main and push
    # /?id=… — regression guard for the dead two-pane "#detail" target
    # (htmx targetError: clicks silently did nothing).
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="search"]')
    await cdp.wait_for_swap(
        mark,
        "!!document.querySelector('#main .find') && !!document.querySelector('#list > *')"
        " && !!document.querySelector('#filters input[name=\"search\"]')",
        "Search rail re-mount loaded the resumed query into #filters and #list",
    )
    # Re-clicking Search now RESUMES the last query (last-selected), and earlier
    # sections left a non-chunks / punctuation state behind — reset to a clean
    # chunks search before driving this section.
    await cdp.eval(
        "(function(){var v=document.querySelector('select[name=\"view\"]');"
        "if(v)v.value='chunks';"
        "var b=document.querySelector('input[name=\"search\"]');"
        "if(b)b.value='';})()"
    )
    mark = await cdp.settle_mark()
    await cdp.click('input[name="search"]')
    await cdp.type_text("needle")
    await cdp.wait_for_swap(
        mark,
        "location.search.includes('q=needle') && !!document.querySelector('#list .search-hit__main')",
        "find keystrokes for 'needle' swapped clickable chunk hits into #list",
    )
    # Slice 3a: the live search state is mirrored into the canonical URL, so a
    # refresh/shared link reproduces the query.
    synced = await cdp.eval("location.search.includes('view=search') && location.search.includes('q=needle')")
    check("search state mirrored into the URL (?view=search&q=needle)", bool(synced))
    mark = await cdp.settle_mark()
    await cdp.click("#list .search-hit__main")
    await cdp.wait_for_swap(mark, "!!document.querySelector('#main .folio')", "find hit click mounted the folio")
    folio = await cdp.eval("!!document.querySelector('#main .folio')")
    pushed = await cdp.eval("location.search.includes('id=')")
    check("find hit click mounts folio in #main", bool(folio))
    check("find hit click pushes /?id= deep link", bool(pushed))

    # Event-precise jump: a search hit opens the folio in TRACE mode (the
    # entry-point rule) anchored at the matched event. The route marks that
    # element .is-target and emits data-scroll-to, which enhance.js consumes
    # (scrollIntoView) then removes — so the reader lands ON the match, not the
    # top. "needle" occurs only in prompts, so every hit carries a prompt
    # event_id → the landing is deterministic. Proves the whole chain (button →
    # route → enhance.js) fires in a real browser under CSP; the consume is also
    # the only in-browser proof scrollToEvent() actually ran. (The settle the
    # wait above counted ran enhance.js first, so the consume is already done.)
    jump_trace = await cdp.eval('!!document.querySelector(\'#main .folio[data-mode="trace"]\')')
    is_target = await cdp.eval("!!document.querySelector('#main .is-target')")
    hint_consumed = await cdp.eval("!document.querySelector('#main [data-scroll-to]')")
    check("find hit opens folio in trace mode", bool(jump_trace))
    check("search jump marks + lands on the matched event",
          bool(is_target) and bool(hint_consumed), f"target={is_target} consumed={hint_consumed}")

    # Slice 3a — the retain crux: Back from the folio restores the prior search
    # results (URL carries the query, so the restore reproduces them).
    restored_search = (
        "location.search.includes('q=needle')"
        " && document.querySelectorAll('#main .search-hit').length > 0"
    )
    await cdp.eval("history.back()")
    await cdp.wait_until(restored_search, "history back from the folio restored the needle results")
    back_url = await cdp.eval("location.search")
    back_hits = await cdp.eval("document.querySelectorAll('#main .search-hit').length")
    back_has_find = await cdp.eval("!!document.querySelector('#main .find')")
    check(
        "back from folio retains the prior search results",
        ("q=needle" in (back_url or "")) and (back_hits or 0) > 0,
        f"url={back_url} hits={back_hits} find={back_has_find}",
    )

    # Slice 3a — the rail-nav path: search → Transcript NAV → Back must also
    # restore the results (htmx tracks the nav's pushed URL, not our replaceState,
    # so this exercises a different snapshot key than the hit-click path above).
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="transcript"]')
    await cdp.wait_for_swap(mark, _main_view_is("transcript"), "rail nav to transcript swapped #main")
    nav_folio = await cdp.eval("!!document.querySelector('#main .folio')")
    await cdp.eval("history.back()")
    await cdp.wait_until(restored_search, "history back from Transcript nav restored the needle results")
    nav_back_url = await cdp.eval("location.search")
    nav_back_hits = await cdp.eval("document.querySelectorAll('#main .search-hit').length")
    check(
        "back after Transcript-nav retains the search results",
        ("q=needle" in (nav_back_url or "")) and (nav_back_hits or 0) > 0,
        f"folio={nav_folio} url={nav_back_url} hits={nav_back_hits}",
    )

    # Slice 3a last-selected: leave search, then RE-CLICK the Search rail item —
    # it resumes the last query (results + URL), not a blank surface.
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="transcript"]')
    await cdp.wait_for_swap(mark, _main_view_is("transcript"), "rail nav to transcript swapped #main")
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="search"]')
    await cdp.wait_for_swap(
        mark,
        "!!document.querySelector('#main .find') && document.querySelectorAll('#main .search-hit').length > 0",
        "Search rail re-click mounted the resumed results",
    )
    resume_hits = await cdp.eval("document.querySelectorAll('#main .search-hit').length")
    resume_url = await cdp.eval("location.search")
    check(
        "re-clicking Search resumes the last query (last-selected)",
        ("q=needle" in (resume_url or "")) and (resume_hits or 0) > 0,
        f"url={resume_url} hits={resume_hits}",
    )

    # sessions view: live zone (loopback server -> live on, sandbox -> empty)
    # over the day-grouped ingested timeline; hist bars scaled by enhance.js
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="sessions"]')
    await cdp.wait_for_swap(mark, _main_view_is("sessions"), "rail nav to sessions swapped #main")
    zones = await cdp.eval(
        "!!document.querySelector('#main .zone--live')"
        " && !!document.querySelector('#main .leaf__head')"
    )
    check("sessions view renders live zone + daybook leaves", bool(zones))
    hist_drawn = await cdp.eval(
        "(function(){var s=document.querySelector('#main .hist span[data-n]');"
        "return s ? s.style.height !== '' : false;})()"
    )
    check("day hist bars scaled by enhance.js", bool(hist_drawn))

    # sub-agent nesting: collapsed by default, chevron expands. Guards the
    # `[hidden]` vs `.row { display:flex }` specificity trap (a class selector
    # beats the UA [hidden] rule) that unit tests can't see — and that the
    # chevron toggle does NOT navigate (stopPropagation keeps the row's hx-get
    # from firing on a caret click).
    sub_row_shown = (
        "(function(){var r=document.querySelector('#main .row--sub');"
        "return r ? (r.offsetParent !== null) : null;})()"
    )
    sub_hidden = await cdp.eval(
        "(function(){var r=document.querySelector('#main .row--sub');"
        "return r ? (r.offsetParent === null) : null;})()"
    )
    check("sub-agents collapsed by default", sub_hidden is True, f"hidden={sub_hidden}")
    await cdp.click("#main .row__toggle")
    sub_shown = await cdp.await_outcome(sub_row_shown)
    still_sessions = await cdp.eval("!!document.querySelector('#main .sessions')")
    check("chevron expands sub-agents", sub_shown is True, f"shown={sub_shown}")
    check("chevron toggle does not navigate away", bool(still_sessions))

    await cdp.click("#main .entries .entry")
    srow = await cdp.await_outcome(f"!!document.querySelector('#main .folio') && {HTMX_IDLE}")
    check("sessions row click mounts folio", bool(srow))

    # workspaces view: the body filter hides master rows. Guards the
    # `.ledger__row[hidden]` vs display:grid trap (a grid display beats the UA
    # [hidden] rule) — same class of bug as the sub-agent rows, invisible to unit
    # tests. Then the recency sort re-render must drop the magnitude bar.
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="workspaces"]')
    await cdp.wait_for_swap(mark, _main_view_is("workspaces"), "rail nav to workspaces swapped #main")
    ws_rows = await cdp.eval(
        "document.querySelectorAll('#main .ledger--ws .ledger__row').length"
    )
    check("workspaces body lists rows", ws_rows is not None and ws_rows >= 2, f"rows={ws_rows}")
    ws_filtered = (
        "(function(){var rows=document.querySelectorAll('#main .ledger--ws .ledger__row');"
        "var shown=0,hid=0;rows.forEach(function(r){"
        "if(r.offsetParent===null)hid++;else shown++;});"
        "return hid>0 && shown>0;})()"
    )
    await cdp.click("#main [data-ws-filter]")
    await cdp.type_text("other")
    filtered = await cdp.await_outcome(ws_filtered)
    check("workspace filter hides non-matching rows", filtered is True, f"ok={filtered}")
    mark = await cdp.settle_mark()
    await cdp.click('#main .ws-sort__opt[hx-get*="sort=recent"]')
    await cdp.wait_for_swap(
        mark, "location.search.includes('sort=recent') && !!document.querySelector('#main .workspaces')",
        "recency sort re-rendered the workspaces view",
    )
    no_bar = await cdp.eval("!document.querySelector('#main .ledger--ws .ledger__bar')")
    still_ws = await cdp.eval("!!document.querySelector('#main .workspaces')")
    check("recency sort drops the magnitude bar", bool(no_bar) and bool(still_ws), f"no_bar={no_bar}")

    # last-selected: the sort lives in the canonical URL, so leaving Workspaces
    # and re-clicking the rail item RESUMES the chosen sort (the bar stays off),
    # not the default sessions order.
    sorted_url = await cdp.eval("location.search")
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="sessions"]')
    await cdp.wait_for_swap(mark, _main_view_is("sessions"), "rail nav to sessions swapped #main")
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="workspaces"]')
    await cdp.wait_for_swap(mark, _main_view_is("workspaces"), "rail nav to workspaces swapped #main")
    ws_resumed_url = await cdp.eval("location.search")
    ws_resumed_nobar = await cdp.eval("!document.querySelector('#main .ledger--ws .ledger__bar')")
    check(
        "re-clicking Workspaces resumes the last sort (last-selected)",
        ws_resumed_url == sorted_url and bool(ws_resumed_nobar),
        f"sorted={sorted_url} resumed={ws_resumed_url} no_bar={ws_resumed_nobar}",
    )

    # stats reckoning: initReck must scale the server-emitted trend bars (set
    # --h under CSP, CSSOM-only) and the Tokens|Cost toggle must re-draw the
    # charts + re-sort the accounts with no round-trip. Invisible to the unit
    # tests (which render markup but never run enhance.js). Guards the new JS.
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="stats"]')
    await cdp.wait_for_swap(mark, _main_view_is("stats"), "rail nav to stats swapped #main")
    bars_scaled = await cdp.eval(
        "(function(){var p=document.querySelector('#main #trend-plot');"
        "if(!p||!p.children.length)return false;"
        "return [].some.call(p.children,function(b){return b.style.getPropertyValue('--h');});})()"
    )
    check("reckoning trend bars scaled by initReck under CSP", bars_scaled is True)
    # the radios are visually hidden (label-styled), so drive the visible label
    by_cost_expr = (
        "!!document.querySelector('#main .reck.by-cost') && "
        "!!document.querySelector('#main #trend-unit') && "
        "/cost/.test(document.querySelector('#main #trend-unit').textContent)"
    )
    await cdp.click('#main .measure label[for="m-cost"]')
    by_cost = await cdp.await_outcome(by_cost_expr)
    check("reckoning measure toggle re-draws to cost", by_cost is True)

    # chart-brushing: clicking a Model-mix name re-renders the reckoning scoped
    # to that model (whole #main swap), marks the row is-current + shows a reset.
    # Guards the htmx wiring + the route's model validation under CSP.
    brushed_expr = (
        "!!document.querySelector('#main .reck__clear') && "
        "!!document.querySelector('#main .ledger--account .ledger__row.is-current')"
    )
    mark = await cdp.settle_mark()
    await cdp.click('#main .reck__books .ledger--account a.ledger__name')
    await cdp.wait_for_swap(mark, "!!document.querySelector('#main .reck__clear')", "model brush re-rendered the reckoning")
    brushed = await cdp.eval(brushed_expr)
    check("model brushing scopes the activity charts", brushed is True)

    # last-selected (3a, generalized): the brush state lives in the canonical URL,
    # so leaving Stats and re-clicking the rail item RESUMES the brushed scope
    # rather than resetting to a bare dashboard.
    brushed_url = await cdp.eval("location.search")
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="sessions"]')
    await cdp.wait_for_swap(mark, _main_view_is("sessions"), "rail nav to sessions swapped #main")
    mark = await cdp.settle_mark()
    await cdp.click('a[data-view="stats"]')
    await cdp.wait_for_swap(mark, _main_view_is("stats"), "rail nav to stats swapped #main")
    resumed_url = await cdp.eval("location.search")
    resumed_scope = await cdp.eval(brushed_expr)
    check(
        "re-clicking Stats resumes the last model-brush (last-selected)",
        resumed_url == brushed_url and resumed_scope is True,
        f"brushed={brushed_url} resumed={resumed_url} scope={resumed_scope}",
    )

    mark = await cdp.settle_mark()
    await cdp.click('#main .reck__clear')
    await cdp.wait_for_swap(
        mark, "!!document.querySelector('#main .reck') && !document.querySelector('#main .reck__clear')",
        "show-all reset re-rendered the unscoped reckoning",
    )
    cleared = await cdp.eval(
        "!document.querySelector('#main .reck__clear') && "
        "!!document.querySelector('#main .reck')"
    )
    check("show-all reset clears the model scope", cleared is True)

    # folio with the rust fence -> Prism autoloader under CSP. The autoloader
    # fetches the grammar after the folio mounts, so the readiness is a token.
    await navigate(
        cdp, f"{BASE}/?id={code_conv}",
        f"!!document.querySelector('#main .folio') && {HTMX_IDLE}"
        " && document.querySelectorAll('#main .token').length > 0",
        "folio mounted and Prism highlighted its code fence",
    )
    pre = await cdp.eval("document.querySelectorAll('#main pre, #main code').length")
    tokens = await cdp.eval("document.querySelectorAll('#main .token').length")
    check("folio code block present", pre and pre > 0, f"pre/code={pre}")
    check("prism highlighted under CSP", tokens and tokens > 0, f"tokens={tokens}")

    # folio reading↔trace toggle: reading mode keeps tool I/O out of the body;
    # clicking Trace re-fetches the folio (the route re-resolves a tools-visible
    # fidelity so get_conversation FETCHES tool input/result) and inlines it.
    # Guards the htmx wiring + the fetch-fidelity resolution under CSP — neither
    # visible to the unit tests (which pass fidelity directly).
    reading_no_tools = await cdp.eval(
        "!document.querySelector('#main .folio[data-mode=\"reading\"] .tool-call')"
    )
    check("folio reading mode keeps tool I/O out of body", bool(reading_no_tools))
    mark = await cdp.settle_mark()
    await cdp.click('#main .folio-mode__btn[hx-get*="mode=trace"]')
    await cdp.wait_for_swap(
        mark, "!!document.querySelector('#main .folio[data-mode=\"trace\"]')",
        "trace toggle re-rendered the folio in trace mode",
    )
    trace_on = await cdp.eval(
        "!!document.querySelector('#main .folio[data-mode=\"trace\"]')"
    )
    trace_tools = await cdp.eval(
        "document.querySelectorAll('#main .folio[data-mode=\"trace\"] .tool-call').length"
    )
    check("folio trace toggle re-renders in trace mode", bool(trace_on))
    check("folio trace mode inlines tool I/O", trace_tools is not None and trace_tools > 0,
          f"tool-calls={trace_tools}")

    # tone toggle (enhance.js listener under CSP)
    before = await cdp.eval("document.body.dataset.tone")
    await cdp.click("[data-tone-toggle]")
    await cdp.await_outcome(f"document.body.dataset.tone !== {json.dumps(before)}")
    after = await cdp.eval("document.body.dataset.tone")
    check("tone toggle flips", before != after, f"{before} -> {after}")


# ---------------------------------------------------------------------------
# Lifecycle + verdict (shell-agnostic)
# ---------------------------------------------------------------------------

try:  # ``python tests/browser_smoke/smoke.py`` vs pytest's namespace import.
    from .remote import (  # type: ignore[import-not-found]
        RemoteConfig,
        RemoteConfigurationError,
        SSHReverseForward,
        redact_endpoint,
        redact_text,
        reserve_loopback_port,
    )
except ImportError:
    from remote import (  # type: ignore[no-redef]
        RemoteConfig,
        RemoteConfigurationError,
        SSHReverseForward,
        redact_endpoint,
        redact_text,
        reserve_loopback_port,
    )


def local_chromium_argv(chromium: str, workdir: Path) -> list[str]:
    """Launch only an isolated local profile and never consult macOS Keychain."""
    return [
        chromium,
        "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--disable-extensions",
        "--use-mock-keychain",
        f"--user-data-dir={workdir / 'profile'}",
    ]


def set_fixture_port(port: int) -> None:
    """Point the unchanged UI flow at the local or forwarded fixture origin."""
    global PORT, BASE
    PORT = port
    BASE = f"http://127.0.0.1:{port}"


# Only what the server process needs to start; everything else is withheld.
FIXTURE_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


def fixture_environment(workdir: Path) -> dict[str, str]:
    """Environment for the fixture server, with nothing from the developer's home.

    On loopback the server enables its live endpoints, which read agent session
    files under HOME and the XDG directories (the sessions live zone, /follow).
    Inherited, those would put real transcripts behind the fixture's port - and,
    in remote mode, through the reverse forward into a shared browser. HOME and
    XDG therefore point inside the workdir, and the rest is an allowlist rather
    than a scrub, so a new SIFTD_* or home-derived variable cannot leak in.
    """
    home = workdir / "home"
    env = {name: os.environ[name] for name in FIXTURE_ENV_PASSTHROUGH if name in os.environ}
    env["HOME"] = str(home)
    for name, relative in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        path = home / relative
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    env["SIFTD_NO_UPDATE_CHECK"] = "1"
    return env


def start_fixture(workdir: Path, port: int) -> tuple[subprocess.Popen[bytes], BinaryIO, str]:
    """Build the isolated fixture and serve it only on local loopback."""
    db_path = workdir / "fixture.db"
    code_conv = build_fixture(db_path)
    server_log = (workdir / "server.log").open("wb")
    siftd_bin = Path(sys.executable).parent / "siftd"
    server = subprocess.Popen(
        [str(siftd_bin), "--db", str(db_path), "serve", "--host", "127.0.0.1", "--port", str(port), "--no-auth"],
        stdout=server_log,
        stderr=subprocess.STDOUT,
        env=fixture_environment(workdir),
    )
    return server, server_log, code_conv


async def wait_for_fixture(port: int) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(40):
            try:
                response = await client.get(f"http://127.0.0.1:{port}/api/v1/health")
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError("fixture server never became healthy; see server.log")


async def check_headers(port: int, check) -> None:
    """Keep HTTP-header assertions on the fixture's own private local socket."""
    async with httpx.AsyncClient() as client:
        print("== headers over real HTTP ==")
        for path in HEADER_PATHS:
            response = await client.get(f"http://127.0.0.1:{port}{path}")
            csp = response.headers.get("content-security-policy", "")
            ok = (
                "default-src 'self'" in csp
                and "connect-src 'self'" in csp
                and all(response.headers.get(key) == value for key, value in EXPECT_HEADERS.items())
            )
            check(
                f"headers on {path}",
                ok,
                f"status={response.status_code}" + ("" if ok else f" csp={csp[:80]!r}"),
            )


# Nothing left loading or in flight: the page has had its chance to raise any
# violation the flow's last action provoked before the verdict reads the sensors.
QUIESCENT = f"document.readyState === 'complete' && {HTMX_IDLE}"


async def run_assertions(cdp, check, code_conv: str) -> None:
    """Run the common T3 instrument, positive control, and complete UI flow.

    Shared by local and remote modes: all waiting goes through ``navigate`` and
    the CDP driver's condition waits, whichever wire the driver reads.
    """
    await cdp.cmd("Page.enable")
    await cdp.cmd("Runtime.enable")
    await cdp.cmd("Log.enable")
    await cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": LISTENER})

    async def violations():
        value = await cdp.eval("JSON.stringify(window.__v || [])")
        return json.loads(value or "[]")

    print("== positive control ==")
    await navigate(cdp, f"{BASE}/")
    await cdp.eval(
        "var s=document.createElement('script');"
        "s.src='https://example.org/x.js';document.head.appendChild(s);'injected'"
    )
    await cdp.await_outcome(
        "(window.__v || []).some(function (v) { return (v.blocked || '').indexOf('example.org') >= 0; })"
    )
    control = await violations()
    control_hit = any("example.org" in (item.get("blocked") or "") for item in control)
    check("positive control blocked+detected", control_hit, json.dumps(control)[:200])
    if not control_hit:
        raise RuntimeError("detector is broken; every negative result would be meaningless")

    print("== shell flow ==")
    try:
        await flow(cdp, check, code_conv)
        await cdp.wait_until(QUIESCENT, "page quiescent (loaded, no htmx request in flight) before the verdict")
    except ReadinessTimeout as error:
        # A UI that never reaches a state the flow needs is a failed check, not
        # a harness fault; the violation verdict below still runs.
        check("flow reached every readiness condition", False, str(error))

    print("== violations ==")
    observed = await violations()
    logs = cdp.security_log_entries()
    check("zero in-page CSP violations", len(observed) == 0, json.dumps(observed)[:400])
    real_logs = [entry for entry in logs if "example.org" not in entry]
    check("zero CDP security-log violations", len(real_logs) == 0, " | ".join(real_logs)[:400])


def result_checker(results):
    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    return check


async def run_local(workdir: Path, chromium: str) -> int:
    """The established local-CDP mode, with a mock Keychain launch flag."""
    # Keep the unmarked remote transport tests importable in CI's dev-only lane.
    # The entrypoint syncs ``serve``, which supplies this local-only dependency.
    import websockets

    results = []
    check = result_checker(results)
    print("== fixture ==")
    server, server_log, code_conv = start_fixture(workdir, PORT)
    print(f"  built fixture.db, code conv {code_conv}")
    chrome = None
    try:
        await wait_for_fixture(PORT)
        await check_headers(PORT, check)
        chrome = subprocess.Popen(
            local_chromium_argv(chromium, workdir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws_url = None
        async with httpx.AsyncClient() as client:
            for _ in range(40):
                try:
                    response = await client.put(f"http://127.0.0.1:{CDP_PORT}/json/new?about:blank")
                    ws_url = response.json()["webSocketDebuggerUrl"]
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.25)
        if not ws_url:
            print("FATAL: chromium CDP endpoint never came up")
            return 2
        async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await run_assertions(cdp, check, code_conv)
        failed = [row for row in results if not row[1]]
        print(f"\n{'SMOKE FAIL' if failed else 'SMOKE PASS'}: {len(results) - len(failed)}/{len(results)}")
        return 1 if failed else 0
    except RuntimeError as error:
        print(f"FATAL: {error}")
        return 2
    finally:
        if chrome:
            chrome.terminate()
            chrome.wait(timeout=10)
        server.terminate()
        server.wait(timeout=10)
        server_log.close()


class SessionWire:
    """Feed page-CDP replies and events into the existing CDP driver.

    A Playwright CDP session delivers only subscribed events, so this list is
    the remote driver's whole view of the page: the security/console sensors
    plus ``Page.loadEventFired``, which ``navigate`` waits on.
    """

    FORWARDED_EVENTS = (
        "Log.entryAdded",
        "Runtime.exceptionThrown",
        "Runtime.consoleAPICalled",
        "Page.loadEventFired",
    )

    def __init__(self, session) -> None:
        self.session = session
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        for method in self.FORWARDED_EVENTS:
            session.on(method, self._event_handler(method))

    def _event_handler(self, method: str):
        def collect(params) -> None:
            self.queue.put_nowait(json.dumps({"method": method, "params": params}))

        return collect

    async def send(self, raw: str) -> None:
        command = json.loads(raw)
        try:
            result = await self.session.send(command["method"], command.get("params", {}))
            reply = {"id": command["id"], "result": result}
        except Exception as error:
            reply = {"id": command["id"], "error": {"message": str(error)}}
        self.queue.put_nowait(json.dumps(reply))

    async def recv(self) -> str:
        return await self.queue.get()


def _write_receipt(artifacts: Path | None, receipt: dict) -> None:
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def source_subject() -> dict[str, str]:
    """Bind remote-fixture evidence to the checked-out source without reading data."""
    root = Path(__file__).resolve().parents[2]
    try:
        return {
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip(),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"head": "unavailable", "tree": "unavailable"}


async def run_remote(workdir: Path, config: RemoteConfig, artifacts: Path | None) -> int:
    """Run T3 via native Playwright and one temporary, verified SSH forward."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("FATAL: remote mode requires the optional browser test dependency; rerun ./dev browser-smoke --remote")
        return 2

    results = []
    check = result_checker(results)
    receipt = {
        "mode": "remote",
        **source_subject(),
        "endpoint": redact_endpoint(config.endpoint),
        "transport": "native Playwright page CDP through SSH reverse loopback forward",
        "local_chromium_launched": False,
        "bypass_csp": False,
        "ignore_https_errors": False,
        "shared_browser_close_called": False,
        "assertions": [],
        "exit": 2,
    }
    tunnel = None
    server = None
    server_log = None
    cdp = None
    try:
        local_port = reserve_loopback_port()
        tunnel = SSHReverseForward(config, local_port)
        remote_port = tunnel.start()
        receipt["local_fixture_port"] = local_port
        receipt["remote_fixture_port"] = remote_port
        set_fixture_port(remote_port)
        print("== fixture ==")
        server, server_log, code_conv = start_fixture(workdir, local_port)
        print(f"  built fixture.db, code conv {code_conv}")
        await wait_for_fixture(local_port)
        await check_headers(local_port, check)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect(config.endpoint, timeout=15_000)
            receipt["browser_version"] = browser.version
            context = await browser.new_context(
                viewport={"width": 800, "height": 600}, bypass_csp=False, ignore_https_errors=False
            )
            receipt["owned_context_closed"] = False
            try:
                page = await context.new_page()
                session = await context.new_cdp_session(page)
                cdp = CDP(SessionWire(session))
                await run_assertions(cdp, check, code_conv)
                await session.detach()
            finally:
                await context.close()
                receipt["owned_context_closed"] = True
        receipt["exit"] = 1 if any(not result[1] for result in results) else 0
    except Exception as error:
        print(f"FATAL: {redact_text(str(error), config.endpoint)}")
    finally:
        receipt["assertions"] = [
            {"name": name, "passed": passed, "detail": detail} for name, passed, detail in results
        ]
        if cdp is not None:
            receipt["cdp_events"] = len(cdp.events)
            if artifacts is not None:
                artifacts.mkdir(parents=True, exist_ok=True)
                (artifacts / "cdp-events.json").write_text(json.dumps(cdp.events, indent=2) + "\n")
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
                receipt["fixture_cleanup_needed_sigkill"] = True
            receipt["fixture_server_exit"] = server.returncode
        if server_log is not None:
            server_log.close()
        if tunnel is not None:
            receipt["remote_listener_cleanup_confirmed"] = tunnel.close()
            receipt["tunnel_exit"] = tunnel.process.returncode if tunnel.process is not None else None
            if not receipt["remote_listener_cleanup_confirmed"]:
                receipt["exit"] = 2
        receipt["passed"] = sum(passed for _, passed, _ in results)
        receipt["failed"] = sum(not passed for _, passed, _ in results)
        _write_receipt(artifacts, receipt)
    print(f"\n{'SMOKE PASS' if receipt['exit'] == 0 else 'SMOKE FAIL'}: {receipt['passed']}/{len(results)}")
    return receipt["exit"]


def prepare_artifacts(path: Path) -> Path:
    """Refuse to overwrite a prior fixture-only smoke receipt."""
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise RemoteConfigurationError(f"artifact directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated T3 browser smoke.")
    parser.add_argument("--remote", action="store_true", help="use the explicit Browserless/SSH fixture route")
    parser.add_argument("--artifacts", type=Path, help="directory for remote fixture-only evidence")
    args = parser.parse_args(argv)
    if args.artifacts is not None and not args.remote:
        parser.error("--artifacts is only valid with --remote")
    if args.remote:
        try:
            config = RemoteConfig.from_environment(dict(os.environ))
        except RemoteConfigurationError as error:
            print(f"FATAL: {error}")
            return 2
        if args.artifacts is not None:
            try:
                artifacts = prepare_artifacts(args.artifacts)
            except RemoteConfigurationError as error:
                print(f"FATAL: {error}")
                return 2
            with tempfile.TemporaryDirectory(prefix="siftd-browser-smoke-") as tmp:
                return asyncio.run(run_remote(Path(tmp), config, artifacts))
        with tempfile.TemporaryDirectory(prefix="siftd-browser-smoke-") as tmp:
            return asyncio.run(run_remote(Path(tmp), config, None))
    chromium = _find_chromium()
    if not chromium:
        print("FATAL: no chromium found — install it or set CHROMIUM_BIN")
        return 2
    with tempfile.TemporaryDirectory(prefix="siftd-browser-smoke-") as tmp:
        return asyncio.run(run_local(Path(tmp), chromium))


if __name__ == "__main__":
    sys.exit(main())
