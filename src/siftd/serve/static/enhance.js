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
    if (c) c.textContent = count || '';
    var k = document.getElementById('sw-kick');
    if (k) k.textContent = kick || '';
    document.querySelectorAll('.sw-nav a[data-view]').forEach(function (a) {
      if (a.getAttribute('data-view') === view) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function enhance() { wireTone(); drawLedgers(); syncChrome(); }

  document.body.addEventListener('htmx:afterSettle', enhance);
  applyTone();
  if (document.readyState !== 'loading') enhance();
  else document.addEventListener('DOMContentLoaded', enhance);
})();
