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

  // --- reckoning (Stats) charts + measure toggle ----------------------------
  // The renderer emits the trend/rhythm bars and the account rows server-side
  // with data-tokens/data-cost; this scales each plot for the active measure,
  // marks the peak, and re-sorts the accounts — so the Tokens|Cost toggle
  // re-draws with no round-trip. CSP-safe: CSSOM .style + classList only.
  // A fresh .reck arrives with every #main swap, so listeners never stack.
  function initReck(root) {
    var reck = (root || document).querySelector('.reck');
    if (!reck) return;
    var trendPlot = reck.querySelector('#trend-plot');
    var hodPlot = reck.querySelector('#hod-plot');
    var dowPlot = reck.querySelector('#dow-plot');
    var peakEl = reck.querySelector('#trend-peak');

    function fmtTok(n) {
      return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : '' + n;
    }
    function fmtCost(n) { return '$' + n.toFixed(2); }

    // round the axis ceiling up to a clean 1/1.5/2/2.5/3/4/5/6/8/10 ×10^n.
    function niceMax(v) {
      if (v <= 0) return 1;
      var base = Math.pow(10, Math.floor(Math.log10(v))), f = v / base;
      var steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
      for (var i = 0; i < steps.length; i++) if (f <= steps[i] + 1e-9) return steps[i] * base;
      return 10 * base;
    }

    function scale(plot, measure) {
      if (!plot) return { peak: null, max: 1 };
      var bars = [].slice.call(plot.children), peakRaw = 0, peak = null;
      bars.forEach(function (b) { var v = +b.dataset[measure] || 0; if (v > peakRaw) peakRaw = v; });
      var max = niceMax(peakRaw);
      bars.forEach(function (b) {
        var v = +b.dataset[measure] || 0;
        b.style.setProperty('--h', Math.max(2, Math.round(v / max * 100)) + '%');
        b.classList.remove('is-peak');
        if (!peak || v > (+peak.dataset[measure] || 0)) peak = b;
      });
      return { peak: peak, max: max };
    }

    function num(row, measure) {
      var v = row.dataset[measure];
      return (v === '' || v == null) ? null : parseFloat(v);
    }
    function sortBook(ul, measure) {
      var rows = [].slice.call(ul.querySelectorAll('.ledger__row:not(.account__row--rest)'));
      var rest = ul.querySelector('.account__row--rest');
      rows.sort(function (a, b) {
        var va = num(a, measure), vb = num(b, measure);
        if (va === null && vb === null) return 0;
        if (va === null) return 1;
        if (vb === null) return -1;
        return vb - va;
      });
      rows.forEach(function (r) { ul.appendChild(r); });
      if (rest) ul.appendChild(rest);
    }

    function setText(id, txt) { var el = reck.querySelector('#' + id); if (el) el.textContent = txt; }

    function recut(measure) {
      reck.classList.remove('by-tokens', 'by-cost');
      reck.classList.add('by-' + measure);
      var t = scale(trendPlot, measure), hh = scale(hodPlot, measure), dd = scale(dowPlot, measure);
      var fmt = measure === 'cost' ? fmtCost : fmtTok, word = measure === 'cost' ? 'cost' : 'tokens';
      setText('trend-ymax', fmt(t.max));
      setText('hod-ymax', fmt(hh.max));
      setText('dow-ymax', fmt(dd.max));
      setText('trend-unit', word + ' per day');
      setText('hod-unit', word + ' per hour');
      setText('dow-unit', word + ' per weekday');
      if (peakEl && t.peak) {
        t.peak.classList.add('is-peak');
        var v = +t.peak.dataset[measure];
        var d = t.peak.dataset.date;
        peakEl.textContent = 'Peak ' + (d ? d + ' · ' : '') + fmt(v);
      }
      reck.querySelectorAll('.ledger--account').forEach(function (ul) { sortBook(ul, measure); });
    }

    reck.querySelectorAll('input[name="measure"]').forEach(function (r) {
      r.addEventListener('change', function () { recut(r.id === 'm-cost' ? 'cost' : 'tokens'); });
    });
    var checked = reck.querySelector('input[name="measure"]:checked');
    recut(checked && checked.id === 'm-cost' ? 'cost' : 'tokens');
  }

  // --- search hit score meters (data-n -> width) ----------------------------
  // Each .hit-meter[data-n] carries score×1000; width is n/10 % (a 0..1 score
  // → 0..100%). CSSOM .style only (CSP), the same data-n→bar idiom as ledgers.
  function drawHitMeters(root) {
    (root || document).querySelectorAll('.hit-meter[data-n]').forEach(function (m) {
      var n = +m.dataset.n || 0;
      m.style.setProperty('--w', Math.max(0, Math.min(100, n / 10)) + '%');
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

  // --- activity scroll-spy (the trace's Activity rail mirrors the body) ------
  // Reverse of the click-to-jump anchors: as the trace body scrolls, mark the
  // Activity row (.tool-seq__row) for the topmost visible tool call .is-current
  // — the same IntersectionObserver idiom as the turns rail above, so the two
  // rails move together. CSP-safe: observer + classList only, no new dep. The
  // rows link to .tool-call[id="evt-N"] (render_folio's Activity registry); a
  // reading-mode folio has no .tool-call[id], so this no-ops there.
  var toolSpyObserver = null;
  function initToolSpy() {
    if (toolSpyObserver) { toolSpyObserver.disconnect(); toolSpyObserver = null; }
    var body = document.querySelector('#main .folio__body');
    if (!body || !('IntersectionObserver' in window)) return;
    var calls = body.querySelectorAll('.tool-call[id]');
    if (!calls.length) return;
    var visible = {};
    function refresh() {
      var bestId = null, bestTop = Infinity;
      calls.forEach(function (c) {
        if (!visible[c.id]) return;
        var top = c.getBoundingClientRect().top;
        if (top < bestTop) { bestTop = top; bestId = c.id; }
      });
      document.querySelectorAll('.tool-seq__row.is-current').forEach(function (r) {
        r.classList.remove('is-current');
      });
      if (bestId) {
        var row = document.querySelector('.tool-seq__row[href="#' + bestId + '"]');
        if (row) row.classList.add('is-current');
      }
    }
    toolSpyObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
      refresh();
    }, { root: body, rootMargin: '0px 0px -55% 0px', threshold: 0 });
    calls.forEach(function (c) { toolSpyObserver.observe(c); });
  }

  // --- search → folio jump: land on the matched event ----------------------
  // A folio mounted from a search hit carries data-scroll-to=<event ULID> on its
  // root; scroll that event's element into view once, then consume the hint so
  // later settles (tag edits, the live poll, an unfold swap) don't re-jump.
  // CSP-safe: querySelector + scrollIntoView, no eval/inline. The matched
  // element also renders .is-target, so it's visually marked once landed.
  function scrollToEvent() {
    var root = document.querySelector('#main [data-scroll-to]');
    if (!root) return;
    var id = root.getAttribute('data-scroll-to');
    root.removeAttribute('data-scroll-to');  // fire once
    if (!id) return;
    var safe = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
    var el = root.querySelector('[data-event-id="' + safe + '"]');
    if (el) el.scrollIntoView({ block: 'start' });
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

  // --- sessions sub-agent toggles -------------------------------------------
  // Parent rows carry a .row__toggle button; clicking it shows/hides that
  // session's nested sub-agent rows (.row--sub[data-parent=<id>]). CSP-safe:
  // attribute/.hidden writes only. stopPropagation keeps the row's hx-get
  // (open folio) from firing when the disclosure caret is clicked.
  function wireSessionToggles(root) {
    (root || document).querySelectorAll('.row__toggle').forEach(function (btn) {
      if (btn._wired) return;
      btn._wired = true;
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var gid = btn.getAttribute('data-group');
        var expanded = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        document.querySelectorAll('.row--sub[data-parent="' + gid + '"]')
          .forEach(function (r) { r.hidden = expanded; });
      });
    });
  }

  // --- workspaces filter ----------------------------------------------------
  // The Workspaces body carries a [data-ws-filter] input; typing hides master
  // rows whose text doesn't match. CSP-safe: .hidden writes only (the row's
  // display:grid is defeated by a .ledger__row[hidden]{display:none} rule, the
  // same fix the sub-agent rows needed). Re-wired per settle; a sort swap
  // replaces the fragment, so the new input starts fresh (filter not preserved).
  function wireWorkspaceFilter(root) {
    (root || document).querySelectorAll('[data-ws-filter]').forEach(function (inp) {
      if (inp._wired) return;
      inp._wired = true;
      var scope = inp.closest('.workspaces') || document;
      inp.addEventListener('input', function () {
        var q = inp.value.trim().toLowerCase();
        scope.querySelectorAll('.ledger--ws .ledger__row').forEach(function (r) {
          if (r.classList.contains('ledger__empty')) return;
          r.hidden = !!q && r.textContent.toLowerCase().indexOf(q) === -1;
        });
      });
    });
  }

  // --- Find: mirror the live search state into the canonical URL (Slice 3a) ---
  // The control strip targets #list (so it never re-renders and keeps focus),
  // which means it can't push a URL via static htmx. After each find swap settles
  // we reconstruct /?view=search&<facets> from the strip and replaceState it — so
  // a refresh reproduces the query, a shared link carries it, and clicking a hit
  // (which pushState's /?id=) leaves a search entry that Back restores with the
  // current facets. replaceState (not push) so refinements don't stack history.
  // CSP-safe: History API + DOM reads only, no eval/inline.
  var FIND_FACETS = [
    ['q', 'search'], ['shape', 'view'], ['engine', 'mode'],
    ['workspace', 'workspace'], ['model', 'model'], ['tag', 'tag'],
    ['tool', 'tool'], ['owner', 'owner'], ['since', 'since'], ['before', 'before'],
    ['sort', 'sort'], ['threshold', 'threshold'], ['full', 'full'],
  ];
  function syncFindUrl() {
    var filters = document.getElementById('filters');
    if (!filters || !document.querySelector('#main .find')) return;
    var url = ['view=search'];      // canonical shell params (q/shape/engine/…)
    var ctrl = [];                  // control params (search/view/mode/…)
    FIND_FACETS.forEach(function (pair) {
      var el = filters.querySelector('[name="' + pair[1] + '"]');
      // A checkbox (full text) reports value '1' regardless of state — read
      // .checked so an unchecked box drops out rather than always carrying.
      var v = el ? (el.type === 'checkbox' ? (el.checked ? '1' : '') : (el.value || '').trim()) : '';
      if (!v) return;
      if (pair[1] === 'view' && v === 'chunks') return;  // default result-shape
      if (pair[1] === 'mode' && v === 'auto') return;     // default engine
      if (pair[1] === 'sort' && v === 'score') return;    // default order
      url.push(pair[0] + '=' + encodeURIComponent(v));
      ctrl.push(pair[1] + '=' + encodeURIComponent(v));
    });
    var shellUrl = '/?' + url.join('&');
    if (location.pathname + location.search !== shellUrl) {
      history.replaceState(history.state, '', shellUrl);
    }
    // Keep the two-stage children's reload targets in step with the live query,
    // so a back/forward history *restore* (which re-fires their hx-trigger=load)
    // reproduces the prefilled strip + results — not the empty initial mount.
    var cqs = ctrl.length ? '?' + ctrl.join('&') : '';
    var list = document.getElementById('list');
    if (list) list.setAttribute('hx-get', '/query' + cqs);
    filters.setAttribute('hx-get', '/meta' + cqs);
    // Last-selected: remember the live query so RE-clicking the Search rail item
    // resumes it (see the capturing handler below). Cleared box → no memory →
    // the nav falls back to a fresh /find.
    // The /find mount takes CANONICAL params (q/shape/engine/…), not the control
    // names (search/view/mode/…) the /meta+/query children speak — so build it
    // from `url` (drop the leading 'view=search'), NOT from cqs.
    var facetParts = url.slice(1);
    var fqs = facetParts.length ? '?' + facetParts.join('&') : '';
    lastSearchMount = fqs ? '/find' + fqs : null;
    lastSearchShell = fqs ? shellUrl : null;
  }

  // Last-selected resume for the Search rail item. It's rendered JS-driven
  // (data-nav-search, no htmx attrs) precisely so htmx's delegated click handler
  // ignores it and there's no race: we own the mount. With a remembered query we
  // resume it (results + canonical URL); otherwise a fresh /find. The mount swaps
  // #main via htmx.ajax and we push the canonical shell URL ourselves. Search's
  // back/forward rides the inline server-render of its shell (not htmx history),
  // which is why it can be JS-driven without losing restore.
  var lastSearchMount = null, lastSearchShell = null;
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-nav-search]');
    if (!a || !window.htmx) return;
    e.preventDefault();
    window.htmx.ajax('GET', lastSearchMount || '/find', { target: '#main', swap: 'innerHTML' });
    history.pushState(history.state, '', lastSearchShell || '/?view=search');
  });

  // Last-selected for the URL-state views (stats ?model=, workspaces ?sort=).
  // Unlike search these stay HTMX-DECLARATIVE so htmx keeps snapshotting #main
  // into history (back/forward restore the prior view). We only REWRITE the rail
  // item's hx-get (mount) + hx-push-url (shell) from the current URL at settle,
  // so re-clicking resumes the live state. A bare view restores the base mount.
  // htmx reads these attributes fresh at click time, so the rewrite takes effect
  // without re-processing the node — and history stays htmx-managed throughout.
  function syncResumeNav() {
    var p = new URLSearchParams(location.search);
    var view = p.get('view');
    // Only rewrite the rail item for the view currently showing — it captures
    // the live state (or clears to base when bare). Leaving the OTHER resumable
    // rails untouched is the whole point: their last-rewritten target must
    // survive while the user is elsewhere, so re-clicking resumes it.
    var a = document.querySelector('[data-resume="' + view + '"]');
    if (!a) return;
    var base = a.getAttribute('data-mount-base') || '/';
    p.delete('view');
    var rest = p.toString();
    a.setAttribute('hx-get', rest ? base + '?' + rest : base);
    a.setAttribute('hx-push-url', rest ? location.pathname + location.search : '/?view=' + view);
  }

  // --- block copy (data-copy-src) --------------------------------------------
  // Copy buttons in trace block panels fetch the STORED block text from the
  // raw-block route and write it to the clipboard — the rendered DOM is not a
  // faithful copy source (markdown re-rendering, presenter line caps). One
  // delegated listener; buttons arrive and leave with htmx swaps for free.
  // Auth rides whatever bearer auth.js stamped on <body hx-headers> — parsed,
  // not reimplemented, so the two stay in step.
  function hxAuthHeaders() {
    try { return JSON.parse(document.body.getAttribute('hx-headers') || '{}'); }
    catch (e) { return {}; }
  }
  function flashCopyState(btn, label) {
    var old = btn.textContent;
    btn.textContent = label;
    if (label === 'Copied') btn.classList.add('is-copied');
    setTimeout(function () {
      btn.textContent = old;
      btn.classList.remove('is-copied');
    }, 1200);
  }
  document.body.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest && e.target.closest('[data-copy-src]');
    if (!btn) return;
    fetch(btn.getAttribute('data-copy-src'), { headers: hxAuthHeaders() })
      .then(function (r) {
        if (!r.ok) throw new Error('fetch ' + r.status);
        return r.text();
      })
      .then(function (text) { return navigator.clipboard.writeText(text); })
      .then(function () { flashCopyState(btn, 'Copied'); })
      .catch(function () { flashCopyState(btn, 'Failed'); });
  });

  // htmx reads hx-push-url fresh at click time but CACHES hx-get (the mount), so
  // syncResumeNav's rewritten mount alone is ignored. configRequest fires before
  // each request with a mutable path — override it from the rail's current hx-get
  // so the resume mount actually lands, while htmx keeps managing history (the
  // snapshot + push). This is why stats/workspaces stay declarative not JS-driven.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (elt && elt.getAttribute && elt.getAttribute('data-resume')) {
      evt.detail.path = elt.getAttribute('hx-get');
    }
  });

  function enhance() { wireTone(); drawLedgers(); drawHists(); drawHitMeters(); initReck(); syncChrome(); initSpy(); initToolSpy(); highlight(); scrollToEvent(); tailLiveFolio(); wireSessionToggles(); wireWorkspaceFilter(); syncFindUrl(); syncResumeNav(); }

  document.body.addEventListener('htmx:afterSettle', enhance);
  applyTone();
  if (document.readyState !== 'loading') enhance();
  else document.addEventListener('DOMContentLoaded', enhance);
})();
