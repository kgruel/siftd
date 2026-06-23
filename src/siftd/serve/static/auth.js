/* siftd browser auth.
 *
 * The browser is a client like the CLI: it ACQUIRES a bearer token and SENDS it;
 * the server only ever VALIDATES. Two acquisition paths, in priority order:
 *
 *   1. OIDC auth-code + PKCE — when the server advertises browser SSO at
 *      GET /auth/config (issuer mode + serve.auth.browser_client_id set). The
 *      browser discovers the IdP's authorize/token endpoints from
 *      issuer/.well-known/openid-configuration and runs the flow entirely
 *      client-side. The server never sees the exchange.
 *   2. Manual bearer paste — always available as a fallback, and the only path
 *      when SSO is disabled (static_token / introspection deployments).
 *
 * The access token lives in sessionStorage and rides every htmx request via
 * body hx-headers. No eval / no inline handlers, so this stays valid under a
 * script-src 'self' 'unsafe-inline' CSP without 'unsafe-eval'.
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'siftd_token';
  var REFRESH_KEY = 'siftd_refresh';
  var PKCE_KEY = 'siftd_pkce';            // {verifier, state, client_id} across the redirect
  var DISCO_KEY = 'siftd_oidc_disco';     // cached {authorization_endpoint, token_endpoint}
  var REFRESH_TRIED_KEY = 'siftd_refresh_tried';

  var callbackInFlight = false;

  // --- token storage ---------------------------------------------------------
  function getToken() { return sessionStorage.getItem(TOKEN_KEY); }
  function setToken(t) { sessionStorage.setItem(TOKEN_KEY, t); }
  function clearTokens() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(REFRESH_KEY);
  }
  function storeTokenResponse(tok) {
    if (tok && tok.access_token) setToken(tok.access_token);
    if (tok && tok.refresh_token) sessionStorage.setItem(REFRESH_KEY, tok.refresh_token);
  }

  // Apply the bearer to every htmx request. Synchronous so it lands before
  // htmx fires the page's hx-trigger="load" requests.
  function applyToken() {
    var t = getToken();
    if (!t) return;
    document.body.setAttribute('hx-headers',
      JSON.stringify({ Authorization: 'Bearer ' + t }));
    if (window.htmx) window.htmx.process(document.body);
  }

  // --- PKCE / base64url helpers ----------------------------------------------
  function b64url(buf) {
    var s = btoa(String.fromCharCode.apply(null, new Uint8Array(buf)));
    return s.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function randomString(nBytes) {
    var a = new Uint8Array(nBytes);
    crypto.getRandomValues(a);
    return b64url(a.buffer);
  }
  async function challengeFor(verifier) {
    var digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    return b64url(digest);
  }

  // --- server-advertised OIDC config + IdP discovery -------------------------
  var _authCfg = null;
  async function authConfig() {
    if (_authCfg) return _authCfg;
    try {
      var r = await fetch('/auth/config', { headers: { Accept: 'application/json' } });
      _authCfg = await r.json();
    } catch (e) {
      _authCfg = { enabled: false };
    }
    return _authCfg;
  }
  async function discover(issuer) {
    var cached = sessionStorage.getItem(DISCO_KEY);
    if (cached) {
      try { return JSON.parse(cached); } catch (e) { /* refetch */ }
    }
    var r = await fetch(issuer.replace(/\/$/, '') + '/.well-known/openid-configuration');
    var d = await r.json();
    var endpoints = {
      authorization_endpoint: d.authorization_endpoint,
      token_endpoint: d.token_endpoint,
    };
    sessionStorage.setItem(DISCO_KEY, JSON.stringify(endpoints));
    return endpoints;
  }
  // Exact origin + path Authentik must have registered as a Redirect URI (this
  // also grants CORS for the token POST below).
  function redirectUri() { return window.location.origin + '/'; }

  async function tokenEndpoint() {
    var cfg = await authConfig();
    if (!cfg.enabled) throw new Error('browser SSO disabled');
    return (await discover(cfg.issuer)).token_endpoint;
  }
  async function postToken(params) {
    var r = await fetch(await tokenEndpoint(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: params.toString(),
    });
    if (!r.ok) throw new Error('token endpoint returned ' + r.status);
    return await r.json();
  }

  // --- flow: kick off login --------------------------------------------------
  // Errors here (IdP unreachable, discovery CORS-blocked, insecure context with
  // no crypto.subtle) must SURFACE — a silent throw leaves the button dead and
  // the misconfig invisible.
  async function startLogin() {
    try {
      var cfg = await authConfig();
      if (!cfg.enabled) return;
      var disco = await discover(cfg.issuer);
      if (!disco.authorization_endpoint || !disco.token_endpoint) {
        throw new Error('IdP discovery is missing authorize/token endpoints');
      }
      var verifier = randomString(48);
      var state = randomString(16);
      sessionStorage.setItem(PKCE_KEY, JSON.stringify({
        verifier: verifier, state: state, client_id: cfg.client_id,
      }));
      var p = new URLSearchParams({
        response_type: 'code',
        client_id: cfg.client_id,
        redirect_uri: redirectUri(),
        scope: cfg.scope,
        state: state,
        code_challenge: await challengeFor(verifier),
        code_challenge_method: 'S256',
      });
      window.location.assign(disco.authorization_endpoint + '?' + p.toString());
    } catch (e) {
      loginError('Could not start SSO login: ' + (e && e.message ? e.message : e) +
        '. Check the IdP is reachable and CORS-permitted; you can paste a token below.');
    }
  }

  // --- flow: handle the redirect back from the IdP ---------------------------
  async function handleCallback() {
    var params = new URLSearchParams(window.location.search);
    var code = params.get('code');
    if (!code) return false;
    var pkce = {};
    try { pkce = JSON.parse(sessionStorage.getItem(PKCE_KEY) || '{}'); } catch (e) { /* */ }
    var cleanUrl = window.location.origin + window.location.pathname;
    // CSRF guard: state must round-trip.
    if (!pkce.state || params.get('state') !== pkce.state) {
      window.history.replaceState({}, '', cleanUrl);
      return false;
    }
    try {
      storeTokenResponse(await postToken(new URLSearchParams({
        grant_type: 'authorization_code',
        code: code,
        redirect_uri: redirectUri(),
        client_id: pkce.client_id,
        code_verifier: pkce.verifier,
      })));
    } catch (e) {
      /* leave unauthenticated — overlay will offer retry */
    } finally {
      sessionStorage.removeItem(PKCE_KEY);
      window.history.replaceState({}, '', cleanUrl);
    }
    return !!getToken();
  }

  // --- flow: silent refresh on 401 ------------------------------------------
  async function tryRefresh() {
    var refresh = sessionStorage.getItem(REFRESH_KEY);
    if (!refresh) return false;
    var cfg = await authConfig();
    if (!cfg.enabled) return false;
    try {
      storeTokenResponse(await postToken(new URLSearchParams({
        grant_type: 'refresh_token',
        refresh_token: refresh,
        client_id: cfg.client_id,
      })));
      return !!getToken();
    } catch (e) {
      return false;
    }
  }

  // --- UI: sign-out button + login overlay -----------------------------------
  function addSignout() {
    // Swiss shell: the rail foot. Falls back to <nav> for any other shell.
    var mount = document.querySelector('.sw-foot') || document.querySelector('nav');
    if (!mount || mount.querySelector('.nav-auth-btn')) return;
    var btn = document.createElement('button');
    btn.className = 'nav-auth-btn';
    btn.textContent = 'Sign out';
    btn.addEventListener('click', function () {
      clearTokens();
      sessionStorage.removeItem(DISCO_KEY);
      location.reload();
    });
    mount.appendChild(btn);
  }

  // Surface a flow error inside the login box (building it first if needed).
  function loginError(msg) {
    var box = document.getElementById('siftd-login');
    if (!box) { showLogin({ sso: true }); box = document.getElementById('siftd-login'); }
    if (!box) return;
    var existing = box.querySelector('.login-error');
    if (existing) { existing.textContent = msg; return; }
    var err = document.createElement('p');
    err.className = 'login-error';
    err.textContent = msg;
    box.appendChild(err);
  }

  function showLogin(opts) {
    if (document.getElementById('siftd-login')) return;
    // Swiss shell mounts the overlay in #main; fall back to #list for any
    // other shell. Without this rebind a 401 leaves a dead login box.
    var mount = document.getElementById('main') || document.getElementById('list');
    if (!mount) return;

    var box = document.createElement('div');
    box.id = 'siftd-login';

    var icon = document.createElement('div');
    icon.className = 'login-icon';
    icon.innerHTML = '&#x1f512;';
    box.appendChild(icon);

    var h = document.createElement('h3');
    h.textContent = 'Sign in to siftd';
    box.appendChild(h);

    if (opts && opts.sso) {
      var sso = document.createElement('button');
      sso.className = 'login-btn';
      sso.textContent = 'Sign in with SSO';
      sso.addEventListener('click', function () { startLogin(); });
      box.appendChild(sso);

      var or = document.createElement('p');
      or.className = 'login-or';
      or.textContent = 'or paste a bearer token';
      box.appendChild(or);
    } else {
      var p = document.createElement('p');
      p.textContent = 'Enter a bearer token to authenticate.';
      box.appendChild(p);
    }

    var form = document.createElement('form');
    var input = document.createElement('input');
    input.type = 'password';
    input.name = 'token';
    input.placeholder = 'Bearer token';
    input.className = 'login-input';
    var submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'login-btn';
    submit.textContent = 'Sign in';
    form.appendChild(input);
    form.appendChild(submit);
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (input.value) { setToken(input.value); location.reload(); }
    });
    box.appendChild(form);

    mount.innerHTML = '';
    mount.appendChild(box);
    input.focus();
    var detail = document.getElementById('detail');
    if (detail) detail.innerHTML = '';
  }

  // --- 401 handling: refresh once, else show the login overlay ---------------
  document.body.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail && e.detail.successful) sessionStorage.removeItem(REFRESH_TRIED_KEY);
  });
  document.body.addEventListener('htmx:responseError', function (e) {
    if (!e.detail || !e.detail.xhr || e.detail.xhr.status !== 401) return;
    if (callbackInFlight) return;                     // mid-login; a reload is coming
    if (document.getElementById('siftd-login')) return;
    (async function () {
      if (!sessionStorage.getItem(REFRESH_TRIED_KEY)) {
        sessionStorage.setItem(REFRESH_TRIED_KEY, '1');
        if (await tryRefresh()) { location.reload(); return; }
      }
      var cfg = await authConfig();
      showLogin({ sso: cfg.enabled });
    })();
  });

  // --- boot ------------------------------------------------------------------
  var hasCode = new URLSearchParams(window.location.search).has('code');
  if (getToken() && !hasCode) {
    applyToken();   // synchronous: header set before htmx's load triggers fire
    addSignout();
  } else if (hasCode) {
    callbackInFlight = true;
    handleCallback().then(function (ok) {
      if (ok) { location.reload(); return; }
      callbackInFlight = false;   // exchange failed — let the next 401 show login
    });
  }
})();
