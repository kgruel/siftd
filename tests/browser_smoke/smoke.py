"""Real-browser CSP smoke (T3 of docs/guides/serve-browser-testing.md).

Run via ``./dev browser-smoke``. Not pytest-collected — this is a standalone
exit-code program: 0 = all checks pass, 1 = check failed, 2 = harness fault
(broken detector, no chromium, server never came up).

The only tier that catches CSP violations from vendored-library *internals*
(htmx's ``new Function`` lives inside htmx.min.js; Prism's autoloader injects
``<script>`` elements at runtime) and real-input behavior (htmx triggers,
inline handlers) — none of which TestClient or the T1/T2 static tiers can see.

Method (each rule exists because its violation produced a false PASS):
- headless Chromium over raw CDP (websockets + httpx — no playwright dep)
- violations detected TWO ways: in-page ``securitypolicyviolation`` listener
  + the CDP security-source log
- POSITIVE CONTROL FIRST: an off-origin <script src> must be blocked and
  detected, else the instrument is broken and every negative is meaningless
- all interactions via Input.dispatchMouseEvent / dispatchKeyEvent — real
  browser input runs handlers in the genuine page world; Runtime.evaluate
  (and element.click() called from it) executes CSP-EXEMPT and falsely passes
- the server is the from-source venv ``siftd`` entrypoint, never the PATH
  binary (a uv-tool snapshot that drifts)

Layout note: the CDP driver, lifecycle, and verdict are shell-agnostic; only
``flow()`` knows the current two-pane shell's selectors. When the Swiss shell
lands, rewrite ``flow()`` (see the swiss variant preserved in project memory)
and leave the rest alone.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

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
        create_database, get_or_create_harness, get_or_create_model,
        get_or_create_provider, get_or_create_workspace, insert_conversation,
        insert_prompt, insert_prompt_content, insert_response,
        insert_response_content,
    )

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

    rebuild_fts_index(conn)
    conn.commit()
    conn.close()
    return ids[2]


# ---------------------------------------------------------------------------
# CDP driver (shell-agnostic)
# ---------------------------------------------------------------------------


class CDP:
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
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=left))
                self.events.append(msg)
            except asyncio.TimeoutError:
                return

    async def eval(self, expr):
        r = await self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

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
"""


# ---------------------------------------------------------------------------
# Flow: the Swiss shell. Shell-SPECIFIC — rewrite when the UI does.
# ---------------------------------------------------------------------------


async def flow(cdp, check, goto, code_conv):
    await goto(f"{BASE}/", 2.5)
    shell_ok = await cdp.eval("!!document.querySelector('.sw-rail') && !!window.htmx")
    check("swiss shell rendered + vendored htmx loaded", bool(shell_ok))

    # rail nav: every view (stubs included) must swap #main via htmx
    for view in ("sessions", "search", "transcript", "tags", "workspaces", "stats"):
        await cdp.click(f'a[data-view="{view}"]')
        await cdp.drain(1.5)
        children = await cdp.eval(
            "document.getElementById('main') ? document.getElementById('main').childElementCount : -1"
        )
        check(
            f"rail nav swaps #main: {view}",
            children is not None and children > 0,
            f"children={children}",
        )

    # find box: real keystrokes -> htmx keyup trigger (350ms delay)
    await cdp.click('a[data-view="search"]')
    await cdp.drain(1.5)
    await cdp.click('input[name="search"]')
    await cdp.type_text("needle")
    await cdp.drain(1.5)
    rows = await cdp.eval(
        "document.querySelectorAll('#list table tbody tr, #list .conv-row, #list tr').length"
    )
    check("find keystrokes filter list", rows is not None and rows > 0, f"rows={rows}")

    # FTS5 footgun characters must not error the list pane
    await cdp.eval("document.querySelector('input[name=\"search\"]').value=''")
    await cdp.click('input[name="search"]')
    await cdp.type_text('a"(:*')
    await cdp.drain(1.5)
    err = await cdp.eval(
        "document.getElementById('list').textContent.toLowerCase().includes('error')"
    )
    check("FTS5 punctuation input survives", not err)

    # row click: a Find list row must mount the folio into #main and push
    # /?id=… — regression guard for the dead two-pane "#detail" target
    # (htmx targetError: clicks silently did nothing).
    await cdp.click('a[data-view="search"]')
    await cdp.drain(1.5)
    await cdp.click("#list tbody tr")
    await cdp.drain(1.5)
    folio = await cdp.eval("!!document.querySelector('#main .folio')")
    pushed = await cdp.eval("location.search.includes('id=')")
    check("find row click mounts folio in #main", bool(folio))
    check("find row click pushes /?id= deep link", bool(pushed))

    # sessions view: live zone (loopback server -> live on, sandbox -> empty)
    # over the day-grouped ingested timeline; hist bars scaled by enhance.js
    await cdp.click('a[data-view="sessions"]')
    await cdp.drain(1.5)
    zones = await cdp.eval(
        "!!document.querySelector('#main .zone--live')"
        " && !!document.querySelector('#main .day__head')"
    )
    check("sessions view renders live zone + day groups", bool(zones))
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
    sub_hidden = await cdp.eval(
        "(function(){var r=document.querySelector('#main .row--sub');"
        "return r ? (r.offsetParent === null) : null;})()"
    )
    check("sub-agents collapsed by default", sub_hidden is True, f"hidden={sub_hidden}")
    await cdp.click("#main .row__toggle")
    await cdp.drain(1.0)
    sub_shown = await cdp.eval(
        "(function(){var r=document.querySelector('#main .row--sub');"
        "return r ? (r.offsetParent !== null) : null;})()"
    )
    still_sessions = await cdp.eval("!!document.querySelector('#main .sessions')")
    check("chevron expands sub-agents", sub_shown is True, f"shown={sub_shown}")
    check("chevron toggle does not navigate away", bool(still_sessions))

    await cdp.click("#main .rows .row")
    await cdp.drain(1.5)
    srow = await cdp.eval("!!document.querySelector('#main .folio')")
    check("sessions row click mounts folio", bool(srow))

    # folio with the rust fence -> Prism autoloader under CSP
    await goto(f"{BASE}/?id={code_conv}", 3.5)
    pre = await cdp.eval("document.querySelectorAll('#main pre, #main code').length")
    tokens = await cdp.eval("document.querySelectorAll('#main .token').length")
    check("folio code block present", pre and pre > 0, f"pre/code={pre}")
    check("prism highlighted under CSP", tokens and tokens > 0, f"tokens={tokens}")

    # tone toggle (enhance.js listener under CSP)
    before = await cdp.eval("document.body.dataset.tone")
    await cdp.click("[data-tone-toggle]")
    await cdp.drain(0.5)
    after = await cdp.eval("document.body.dataset.tone")
    check("tone toggle flips", before != after, f"{before} -> {after}")


# ---------------------------------------------------------------------------
# Lifecycle + verdict (shell-agnostic)
# ---------------------------------------------------------------------------


async def run(workdir: Path, chromium: str) -> int:
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    print("== fixture ==")
    db_path = workdir / "fixture.db"
    code_conv = build_fixture(db_path)
    print(f"  built {db_path.name}, code conv {code_conv}")

    print("== server (from-source venv entrypoint) ==")
    siftd_bin = Path(sys.executable).parent / "siftd"
    server = subprocess.Popen(
        [str(siftd_bin), "--db", str(db_path), "serve",
         "--host", "127.0.0.1", "--port", str(PORT), "--no-auth"],
        stdout=(workdir / "server.log").open("w"), stderr=subprocess.STDOUT,
    )
    chrome = None
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(40):
                try:
                    r = await client.get(f"{BASE}/api/v1/health")
                    if r.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            else:
                print("FATAL: server never became healthy; see server.log")
                return 2

            # ---- real-HTTP header assertions (per-app middleware, F3) ----
            print("== headers over real HTTP ==")
            for path in HEADER_PATHS:
                r = await client.get(f"{BASE}{path}")
                csp = r.headers.get("content-security-policy", "")
                ok = (
                    "default-src 'self'" in csp
                    and "connect-src 'self'" in csp
                    and all(r.headers.get(k) == v for k, v in EXPECT_HEADERS.items())
                )
                check(f"headers on {path}", ok,
                      f"status={r.status_code}" + ("" if ok else f" csp={csp[:80]!r}"))

        chrome = subprocess.Popen(
            [chromium, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
             "--no-first-run", "--disable-extensions",
             f"--user-data-dir={workdir / 'profile'}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ws_url = None
        async with httpx.AsyncClient() as client:
            for _ in range(40):
                try:
                    r = await client.put(f"http://127.0.0.1:{CDP_PORT}/json/new?about:blank")
                    ws_url = r.json()["webSocketDebuggerUrl"]
                    break
                except Exception:
                    await asyncio.sleep(0.25)
        if not ws_url:
            print("FATAL: chromium CDP endpoint never came up")
            return 2

        async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.cmd("Page.enable")
            await cdp.cmd("Runtime.enable")
            await cdp.cmd("Log.enable")
            await cdp.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": LISTENER})

            async def goto(url, settle=2.0):
                await cdp.cmd("Page.navigate", {"url": url})
                await cdp.drain(settle)

            async def violations():
                v = await cdp.eval("JSON.stringify(window.__v || [])")
                return json.loads(v or "[]")

            # ---- positive control: instrument must catch a blocked script ----
            print("== positive control ==")
            await goto(f"{BASE}/", 2.5)
            await cdp.eval(
                "var s=document.createElement('script');"
                "s.src='https://example.org/x.js';document.head.appendChild(s);'injected'"
            )
            await cdp.drain(1.0)
            ctrl = await violations()
            ctrl_hit = any("example.org" in (v.get("blocked") or "") for v in ctrl)
            check("positive control blocked+detected", ctrl_hit, json.dumps(ctrl)[:200])
            if not ctrl_hit:
                print("FATAL: detector is broken; every negative result would be meaningless")
                return 2

            print("== shell flow ==")
            await goto(f"{BASE}/", 2.5)  # fresh document, control injection gone
            await flow(cdp, check, goto, code_conv)

            # ---- verdict ----
            print("== violations ==")
            v = await violations()
            logs = cdp.security_log_entries()
            check("zero in-page CSP violations", len(v) == 0, json.dumps(v)[:400])
            real_logs = [entry for entry in logs if "example.org" not in entry]
            check("zero CDP security-log violations", len(real_logs) == 0,
                  " | ".join(real_logs)[:400])

        failed = [r for r in results if not r[1]]
        print(f"\n{'SMOKE FAIL' if failed else 'SMOKE PASS'}: {len(results) - len(failed)}/{len(results)}")
        return 1 if failed else 0
    finally:
        if chrome:
            chrome.terminate()
        server.terminate()
        server.wait(timeout=10)


def main() -> int:
    chromium = _find_chromium()
    if not chromium:
        print("FATAL: no chromium found — install it or set CHROMIUM_BIN")
        return 2
    with tempfile.TemporaryDirectory(prefix="siftd-browser-smoke-") as tmp:
        return asyncio.run(run(Path(tmp), chromium))


if __name__ == "__main__":
    sys.exit(main())
