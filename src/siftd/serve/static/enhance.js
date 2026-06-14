/* enhance.js — Swiss shell glue.
 *
 * Three jobs, all CSP-safe (no eval, no inline-style attributes — only CSSOM
 * `.style` writes, which `style-src` does not govern; see the browser-csp-smoke
 * methodology):
 *   1. tone — light/dark, persisted in localStorage, toggled from the rail foot
 *   2. ledger bars — read each .ledger__bar[data-n], set --w proportionally
 *   3. chrome head + active nav — read the swapped fragment's data-* on every
 *      htmx settle so the view header and rail highlight follow #main, without
 *      coupling the fragment to the head via hx-swap-oob.
 *   4. transcript scroll-spy — an IntersectionObserver marks the rail's current
 *      turn (.is-current) as the folio body scrolls.
 */
(function () {
  'use strict';

  // --- tone ------------------------------------------------------------------
  function applyTone() {
    var t = localStorage.getItem('siftd-tone') || 'light';
    document.body.dataset.tone = t;
    var btn = document.querySelector('[data-tone-toggle]');
    if (btn) btn.textContent = (t === 'dark') ? 'Light' : 'Dark';
  }
  function wireTone() {
    var btn = document.querySelector('[data-tone-toggle]');
    if (!btn || btn._wired) return;
    btn._wired = true;
    btn.addEventListener('click', function () {
      var next = (document.body.dataset.tone === 'dark') ? 'light' : 'dark';
      localStorage.setItem('siftd-tone', next);
      applyTone();
    });
  }

  // --- ledger bars (data-n -> --w) ------------------------------------------
  function drawLedgers(root) {
    (root || document).querySelectorAll('.ledger').forEach(function (list) {
      var bars = list.querySelectorAll('.ledger__bar[data-n]');
      var max = 1;
      bars.forEach(function (b) { var n = +b.dataset.n || 0; if (n > max) max = n; });
      bars.forEach(function (b) {
        var n = +b.dataset.n || 0;
        b.style.setProperty('--w', Math.round(n / max * 100) + '%');
      });
    });
  }

  // --- day histograms (data-n -> height) ------------------------------------
  // Sessions day-heads render 24 hour-bucket spans server-side; scale each
  // against the day's max. CSSOM .style writes only (CSP: no inline styles).
  function drawHists(root) {
    (root || document).querySelectorAll('.hist').forEach(function (h) {
      var spans = h.querySelectorAll('span[data-n]');
      var max = 1;
      spans.forEach(function (s) { var n = +s.dataset.n || 0; if (n > max) max = n; });
      spans.forEach(function (s) {
        var n = +s.dataset.n || 0;
        s.style.height = (n ? Math.max(2, Math.round(n / max * 16)) : 1) + 'px';
      });
    });
  }

  // --- chrome head + active nav from the mounted fragment -------------------
  function syncChrome() {
    var v = document.querySelector('#main [data-view]');
    if (!v) return;
    var view = v.getAttribute('data-view');
    var title = v.getAttribute('data-title');
    var count = v.getAttribute('data-count');
    var kick = v.getAttribute('data-kick');
    var h = document.getElementById('sw-title');
    if (h && title) h.textContent = title;
    var c = document.getElementById('sw-count');
    if (c) {
      if (!count && view === 'search') {
        // Find sets no data-count on its host (the result list loads as a
        // separate #list fragment). Derive the shown count from the rendered
        // rows once they settle — honest "showing N" for the paged list.
        var n = document.querySelectorAll('#main #list .conversation-list tbody tr').length;
        count = n ? String(n) : '';
      }
      c.textContent = count || '';
    }
    var k = document.getElementById('sw-kick');
    if (k) k.textContent = kick || '';
    document.querySelectorAll('.sw-nav a[data-view]').forEach(function (a) {
      if (a.getAttribute('data-view') === view) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  // --- transcript scroll-spy (rail .is-current follows the body) ------------
  // CSP-safe: IntersectionObserver + classList, no eval/inline. The folio body
  // is its own scroll container, so it's the observer root; the negative bottom
  // margin makes "current" track the turn crossing the top third of the view.
  var spyObserver = null;
  function initSpy() {
    if (spyObserver) { spyObserver.disconnect(); spyObserver = null; }
    var body = document.querySelector('#main .folio__body');
    if (!body || !('IntersectionObserver' in window)) return;
    var turns = body.querySelectorAll('.turn[id]');
    if (!turns.length) return;
    var visible = {};
    function refresh() {
      var bestId = null, bestN = Infinity;
      Object.keys(visible).forEach(function (id) {
        if (!visible[id]) return;
        var n = parseInt(id.replace('t-', ''), 10);
        if (n < bestN) { bestN = n; bestId = id; }
      });
      document.querySelectorAll('.turn-item.is-current').forEach(function (a) {
        a.classList.remove('is-current');
      });
      if (bestId) {
        var item = document.querySelector('.turn-item[href="#' + bestId + '"]');
        if (item) item.classList.add('is-current');
      }
    }
    spyObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
      refresh();
    }, { root: body, rootMargin: '0px 0px -65% 0px', threshold: 0 });
    turns.forEach(function (t) { spyObserver.observe(t); });
  }

  // --- prism syntax highlighting --------------------------------------------
  // The folio's markdown emitter produces fenced <pre><code class="language-*">
  // blocks; prism (vendored, autoloader-driven) colorizes them. CSP-safe: prism
  // ships as static <script> includes, no eval on our side.
  function highlight() {
    if (window.Prism) window.Prism.highlightAll();
  }

  // --- live folio tail ------------------------------------------------------
  // /follow self-refreshes as a whole-folio outerHTML swap, which resets the
  // body's scroll each poll. Capture scroll state before the swap, restore it
  // after: pinned to the bottom when the reader was at (or near) it — it's a
  // tail — otherwise back to where they were reading. A first mount (no prior
  // live folio) starts pinned.
  var liveScroll = null;
  document.body.addEventListener('htmx:beforeSwap', function () {
    var body = document.querySelector('.folio--live .folio__body');
    if (!body) return;
    liveScroll = {
      top: body.scrollTop,
      stick: body.scrollTop + body.clientHeight >= body.scrollHeight - 40,
    };
  });
  function tailLiveFolio() {
    var body = document.querySelector('.folio--live .folio__body');
    if (!body) { liveScroll = null; return; }
    body.scrollTop = (liveScroll === null || liveScroll.stick)
      ? body.scrollHeight : liveScroll.top;
    liveScroll = null;
  }

  function enhance() { wireTone(); drawLedgers(); drawHists(); syncChrome(); initSpy(); highlight(); tailLiveFolio(); }

  document.body.addEventListener('htmx:afterSettle', enhance);
  applyTone();
  if (document.readyState !== 'loading') enhance();
  else document.addEventListener('DOMContentLoaded', enhance);
})();
