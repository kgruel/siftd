# Serve browser testing — CSP smoke methodology + tiers

Status: **all three tiers implemented.** Written 2026-05-24 after a manual
browser CSP smoke of the serve UI found a real bug; tiers landed 2026-06-10.

- **T1 + T2** — `tests/architecture/test_csp_fitness.py`. The text-scan checks
  (no-eval-requiring-construct, no-eval-in-first-party-JS) have no optional
  import and run in the base lane on every `./dev check`. The checks that
  import `siftd.serve` (CSP pin, shell↔CSP cross-checks) `importorskip`
  litestar, so they execute wherever the `serve` extra is installed (CI's
  serve/embed jobs) and skip in a bare base-lane run.
- **T3** — `./dev browser-smoke` → `tests/browser_smoke/smoke.py` (manual,
  pre-merge: serve-layer changes touching headers/CSP/UI JS — see
  `tests/README.md`; no CI job runs it)

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
under `'unsafe-inline'`, no eval). The CSP itself shipped with the 0.9.x
security hardening (audit findings F2-F9 in the CHANGELOG); this bug surfaced
under it later, which is what motivated the browser tier below.

General rule this implies: **every JS construct the page emits must be permitted
by the CSP it ships.** With `'unsafe-eval'` absent, that bans `hx-on`,
`hx-vals="js:…"`, and `hx-trigger` event-filter brackets (`[expr]`) — all of
which htmx evaluates via `new Function`.

### ⚠ connect-src and browser SSO

The browser login flow (`static/auth.js`) does two cross-origin requests to the
IdP: discovery (`GET issuer/.well-known/...`) and the PKCE token exchange
(`POST token_endpoint`). The policy is `default-src 'self'` with `connect-src`
scoped to `'self'` — and `_build_csp()` (`src/siftd/serve/app.py`) widens
`connect-src` to the configured `serve.auth.issuer` origin whenever one is set,
so those fetches are allowed. **Don't drop that widening** — without it SSO
login breaks with a CSP error and the "Sign in with SSO" button dead-ends (now
surfaced via `loginError()`, not silent). The token POST also requires CORS
from the IdP regardless of CSP (granted by registering the serve origin as a
Redirect URI). The sessionStorage token store `auth.js` uses leans on
`script-src` keeping XSS out — so the CSP and the login flow are coupled: don't
tighten one without checking the other.

## How to run a trustworthy browser CSP smoke

Manual recipe (Chromium driven over the DevTools Protocol; `websockets` + `httpx`
are already dev deps, no Playwright required):

1. **Run the server from source**, not the installed `siftd` binary (which is a
   uv-tool snapshot and drifts). Loopback + `--no-auth` is fine for a local smoke
   (the public-bind guard only blocks non-loopback). `--db` is a global flag:
   `uv run siftd --db /tmp/smoke.db serve --host 127.0.0.1 --port N --no-auth`
   (run from the repo root, so `uv run` resolves the in-tree source). The
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

- **T1 — static fitness function (no browser).** Assert the sources that emit
  HTML contain no eval-requiring htmx construct (`hx-on`, `hx-vals="js:"`,
  `hx-trigger` event-filter brackets) while the CSP lacks `'unsafe-eval'` — this
  text scan has no optional import and runs in the base CI lane, near-zero
  cost, and would have caught the motivating bug. It's paired with a pin on the
  CSP header value so policy changes are deliberate; the pin renders the shell
  and imports `siftd.serve`, so (like T2) it only runs where the `serve` extra
  is installed. Mirrors the existing `tests/architecture/test_mock_ratchet.py`
  ratchet pattern. **Recommended first; small enough to ride alongside the fix
  it guards.**
- **T2 — HTML↔CSP cross-check (no browser).** Parse the served HTML and validate
  all authored script/style/img `src`s and inline handlers against the declared
  CSP directives. More general than T1 (also catches off-origin asset
  reintroduction); still no browser.
- **T3 — real-browser smoke (`./dev browser-smoke` → `tests/browser_smoke/smoke.py`,
  the CDP recipe above).** The only tier that catches violations from
  **vendored-library internals** — htmx's `new Function` lives inside
  `htmx.min.js`; here the *trigger* was an authored `hx-on`, so T1 suffices, but
  a vendored plugin that internally evals would only surface in a browser.
  Closes audit gaps G2 (serve-acceptance) and R5 (browser-under-CSP untested).

The authored-construct class (T1/T2) covers the common case and the known bug;
its text-scan checks run in the base CI lane on every `./dev check`, and its
`siftd.serve`-importing checks run wherever the `serve` extra is installed. T3
is for library-internal risk; it's a manual pre-merge check for serve-layer
changes touching headers/CSP/UI JS rather than a CI job, since it needs a
browser dependency and carries async/timing flakiness.
