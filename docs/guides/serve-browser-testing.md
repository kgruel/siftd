# Serve browser testing — CSP smoke methodology + roadmap

Status: **roadmap / methodology note.** Seeds a future browser-testing branch.
Not yet implemented as automated tests. Written 2026-05-24 after a manual
browser CSP smoke of the serve UI found a real bug.

## Why this exists

The serve test suite (`tests/test_serve*.py`) is entirely `TestClient`-based:
it asserts on HTTP responses — status, headers, body — but **never executes the
page's JavaScript in a browser**. That makes a whole bug class structurally
invisible: *the Content-Security-Policy forbidding client JS that the page needs
to run.* Anything that only manifests when a real browser runs the served JS
under the CSP — htmx behaviors, Prism, inline handlers, eval dependencies, the
autoloader's dynamic script injection — passes the suite regardless.

This gap is real, not hypothetical. The CSP added in the serve security audit
(finding F3) silently broke a UI feature, and it shipped through the original
audit **and two deep re-verification loops** undetected, because nothing in the
suite runs a browser.

### The motivating bug (fixed)

The CSP is `script-src 'self' 'unsafe-inline'` — deliberately **no
`'unsafe-eval'`**. htmx compiles `hx-on` attribute bodies via `new Function`,
which `'unsafe-eval'` governs. The shell's "Recent" nav link used
`hx-on::before-request` to clear the filter controls; under the CSP that handler
threw a `script-src` eval violation, never ran, and left stale filters applied
(plus a console error per click). Fixed by reauthoring it as a normal
`htmx:before-request` `addEventListener` in an inline `<script>` block (allowed
under `'unsafe-inline'`, no eval). See
`security/260524-1342-serve-api-security-audit/` for the audit context.

General rule this implies: **every JS construct the page emits must be permitted
by the CSP it ships.** With `'unsafe-eval'` absent, that bans `hx-on`,
`hx-vals="js:…"`, and `hx-trigger` event-filter brackets (`[expr]`) — all of
which htmx evaluates via `new Function`.

## How to run a trustworthy browser CSP smoke

Manual recipe (Chromium driven over the DevTools Protocol; `websockets` + `httpx`
are already dev deps, no Playwright required):

1. **Run the server from source**, not the installed `siftd` binary (which is a
   uv-tool snapshot and drifts). Loopback + `--no-auth` is fine for a local smoke
   (the public-bind guard only blocks non-loopback). `--db` is a global flag:
   `siftd --db /tmp/smoke.db serve --host 127.0.0.1 --port N --no-auth`. The
   server auto-creates the schema, so a fresh temp DB works. To exercise the
   Prism autoloader you need a conversation whose response contains a fenced code
   block in a non-core language (e.g. ```rust```); the detail view auto-loads at
   `/?id=<ULID>` (siftd's resolvable id, not the adapter `external_id`).
2. Launch `chromium --headless=new --remote-debugging-port=N`; resolve a tab via
   `PUT /json/new?<url>` → `webSocketDebuggerUrl`; subscribe to `Log.entryAdded`,
   `Runtime.consoleAPICalled`, `Runtime.exceptionThrown`.
3. Detect violations two ways: an in-page `securitypolicyviolation` listener and
   the CDP security-source log.

### Three gotchas — each produced a false "it works"

These are the difference between a smoke test that lies and one that doesn't:

- **CDP `Runtime.evaluate` bypasses the page CSP for eval.** Running
  `new Function`/`eval` via `Runtime.evaluate`, or via a `<script>` element it
  injects, or via `Page.addScriptToEvaluateOnNewDocument`, executes in a
  CSP-exempt world and falsely succeeds.
- **Scripted clicks inherit the bypass.** `element.click()` called from
  `Runtime.evaluate` runs the element's *synchronous* handlers (e.g. htmx's
  `hx-on` → `new Function`) in that same exempt world. **Trigger real
  interactions with `Input.dispatchMouseEvent`** (press + release at the
  element's bounding-box center) — browser-level input runs handlers in the
  genuine page world under real CSP enforcement. This is the single most
  important rule.
- **Validate the instrument with a positive control.** Inject an off-origin
  `<script src=https://example.org/x.js>`; it *must* be blocked and caught by
  your detector. If it isn't, the detector is broken and every negative result is
  meaningless. Cross-check enforcement direction with a control page served by
  Python's `http.server` setting the exact CSP header.

## Tiered test strategy

From cheapest/narrowest to most thorough:

- **T1 — static fitness function (no browser).** Render the shell; assert it
  contains no eval-requiring htmx construct (`hx-on`, `hx-vals="js:"`,
  `hx-trigger` event-filter brackets) while the CSP lacks `'unsafe-eval'`; pin the
  CSP header value so policy changes are deliberate. Mirrors the existing
  `tests/architecture/test_mock_ratchet.py` ratchet pattern. Runs in the base CI
  lane, near-zero cost, and would have caught the motivating bug. **Recommended
  first; small enough to ride alongside the fix it guards.**
- **T2 — HTML↔CSP cross-check (no browser).** Parse the served HTML and validate
  all authored script/style/img `src`s and inline handlers against the declared
  CSP directives. More general than T1 (also catches off-origin asset
  reintroduction); still no browser.
- **T3 — real-browser smoke (Playwright or the CDP recipe above).** The only tier
  that catches violations from **vendored-library internals** — htmx's
  `new Function` lives inside `htmx.min.js`; here the *trigger* was an authored
  `hx-on`, so T1 suffices, but a future vendored plugin that internally evals
  would only surface in a browser. Cost: a browser dependency in CI and
  async/timing flakiness. Closes audit gaps G2 (serve-acceptance) and R5
  (browser-under-CSP untested); see `docs/dev/test-refactor-plan.md` Phase 2.

The authored-construct class (T1/T2) covers the common case and the known bug;
T3 is for library-internal risk and is a deliberate cost/benefit call given the
UI surface is small and changes rarely.
