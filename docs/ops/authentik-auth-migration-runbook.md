# Authentik → siftd: Device-Code Login (§5.2) + JWKS Migration (§5.1)

**Operator:** homelab AI agent with admin access to the **live Authentik 2025.x** instance currently serving siftd in **introspection** mode behind Caddy.

**Goal (one session, one shared provider/application):**
1. **§5.2** — enable device-code login for the siftd CLI (turn on the device-code flow brand-wide + make the siftd provider a **Public** client).
2. **§5.1** — migrate siftd serve from **introspection → JWKS**: the provider issues *signed* JWT access tokens; siftd validates them against the issuer's JWKS.

Both share the **existing siftd provider/application**. You edit it in place (preserves `client_id`, so the audience and issuer slug stay stable).

---

## Operating rules

- **Work top to bottom. Do not skip a ✅ gate.** Each gate makes the next change safe to verify. A skipped gate makes later failures misattribute.
- **🛑 STOP-ASK-HUMAN** markers are decisions an agent must not guess (which signing cert, whether the provider also fronts browser SSO, rollback timing).
- **Never paste a real JWT into a web tool** (jwt.io etc.). Decode locally with the `python3` one-liner in §V2.
- **Idempotent intent:** every "set X" means *ensure X equals this value* — re-running must not create duplicates.
- After each provider change, the **discovery doc + a real token** are the source of truth, not the UI. Verify, don't assume.
- ⚠ marks UI-label / placement details that drift across 2025.x point releases — locate the field by **function**, confirm on your instance.

---

## Phase 0 — Pre-flight gates (collect / confirm before any change)

| Item | Why it blocks | How to confirm |
|---|---|---|
| **The siftd provider is API-token-only**, NOT also fronting Caddy `forward_auth` browser SSO. 🛑 | You will flip it to **Public** (§5.2). If the *same* provider also does confidential browser/proxy SSO, flipping breaks SSO. | Check the Caddyfile: the proven `smoke/homelab/Caddyfile` uses plain `reverse_proxy` (no `forward_auth`) → safe. If a `forward_auth`/proxy provider points at this same OAuth2 provider, **STOP** — do not flip; ask the human (a dedicated second public provider would be needed, which is out of scope for this one-provider migration). |
| **Provider `Issuer mode` = "Each provider has a different issuer, based on the application slug" (Per Provider).** | siftd's `auth.issuer`, server `[serve.auth] issuer`, and the JWKS-origin check all depend on the per-slug issuer `https://<host>/application/o/<slug>/`. If mode = **Global**, the issuer becomes the host root and every assumption breaks. | Applications > Providers > *(siftd)* > Edit → confirm Issuer mode. |
| **Caddy forwards public Host + `X-Forwarded-Proto`.** | Authentik builds issuer/jwks_uri/endpoints from the *inbound request* (`build_absolute_uri`). Wrong forwarded host/scheme → discovery advertises `http://`/internal URLs → issuer mismatch + JWKS-origin failure. Fix the proxy, never override endpoints in siftd. | `curl -s https://<host>/application/o/<slug>/.well-known/openid-configuration \| jq .issuer` must show your **public https** origin. |
| **`ISSUER_URL`** captured. | Used verbatim in 4 places. | `ISSUER_URL = https://<host>/application/o/<slug>/` — `<slug>` is the **Application** slug bound to the provider (not the provider name). **Trailing slash included** (Authentik emits it). |
| **Signing certificate choice** 🛑 | Determines RS256 vs ES256. | RSA cert → **RS256** (safe default; the built-in *"authentik Self-signed Certificate"* works). EC cert MUST be **P-256/SECP256R1** to yield ES256 (P-384→ES384, P-521→ES512 — siftd validates only RS256/ES256). |
| **Rollback window agreed** 🛑 | Migration touches the live auth path. | Confirm a maintenance window and that you can restart `siftd serve` (the rollback in §R requires it). |

✅ **Gate 0:** all rows have concrete values; the provider-is-API-only and Issuer-mode-Per-Provider checks both pass. If the provider also fronts browser SSO, **stop here**.

---

## Phase 1 — Provider changes (the OAuth2/OIDC provider feeding siftd)

> All in **Applications > Providers > *(siftd provider)* > Edit**. ⚠ Several of these live under an **"Advanced protocol settings"** expander whose contents shift across 2025.x; if you don't see a field, expand all sections.

Each change is tagged to the exact siftd contract it satisfies.

### 1.1 Client type = **Public** → satisfies §5.2 (siftd sends `client_id` with no secret)
Set **Client type** to **Public**. Public clients are secret-less by design; the device-code token exchange enforces `client_secret` only for *Confidential* clients (PR #21700 gated the check on `CONFIDENTIAL`; public clients are exempt). This is also why JWKS-over-introspection is the right call here — Authentik's introspection endpoint had a bug validating `client_secret` for public providers (#11616).

### 1.2 Signing Key = your chosen cert → satisfies §5.1 (RS256/ES256 signed JWT)
Set **Signing Key** to the RSA (or P-256 EC) cert from Gate 0.
- With a signing key set: `jwt_key` returns `(private_key, RS256/ES256)` → the access token is a **verifiable JWT**, public key published at the JWKS endpoint.
- With **no** signing key: Authentik signs **symmetrically (HS256)** with the client secret → siftd's JWKS path **fails closed** (it only accepts RS256/ES256, `auth.py:217`).

### 1.3 Encryption Key = **empty** → satisfies §5.1 (plain signed JWT, not JWE)
Ensure **Encryption Key** is **unset**. If set, Authentik emits **JWE** (RSA-OAEP-256 + A256CBC-HS512), which siftd cannot validate as a signed JWT.
⚠ **2025.12 placement change:** PR #17722 moved the Encryption-Key field in the 2025.12 series. Find it by function wherever it now renders and confirm it is empty.

### 1.4 Subject mode → defines siftd's identity (`identity_claim` default `sub`)
siftd's JWT path uses `identity_claim = "sub"` by default (`auth.py:201`), and that `sub` becomes the per-conversation owner in siftd. Authentik's **Subject mode** defaults to **hashed_user_id** (opaque but stable). For a human-readable owner, set Subject mode to **user_email** or **user_username**.
🛑 Decision: keep `hashed_user_id` (stable, opaque) or switch to email/username. Whatever you pick is what shows up in siftd's `conversation_owners` — pick once, before first push, to avoid split identities.

### 1.5 Selected Scopes → must include **offline_access** → satisfies refresh-token auto-renew
Under **Selected Scopes**, ensure both are present:
- `authentik default OAuth Mapping: OpenID 'openid'`
- `authentik default OAuth Mapping: OpenID 'offline_access'`  ⚠ confirm exact display label on your instance

Since 2024.2 the device-code response returns **access_token + id_token only**; the refresh token is issued **only** when `offline_access` is selected on the provider **and** requested by the client. siftd's client default scope is `"openid offline_access"`, so the client side is covered — the provider side is the missing half. The refresh grant itself rejects with `invalid_scope` if `offline_access` is absent from the token (`token.py @2025.12.6`).

> If you defined custom siftd read/write scopes (e.g. `siftd:read`, `siftd:write`) via **Customization > Property Mappings > Create > Scope Mapping**, add them to Selected Scopes here too, and add them to the client's `auth.scope`. They surface in the token's space-delimited `scope` claim, which is what `[serve.auth] required_scopes` / `write_scopes` validate.

### 1.6 Read back the **Client ID** → becomes `auth.client_id` (client) and likely `audience` (server)
Note the provider's **Client ID** (e.g. `siftd-cli`, or the auto-generated value). This is siftd's `auth.client_id`, and — per source — also the access token's `aud` (set at `IDToken.new()`, `aud = provider.client_id`). You will **confirm** `aud` against a decoded token in §V6 before trusting it for the server config.

✅ **Gate 1:** Client type = Public; Signing Key set; Encryption Key empty; Subject mode decided; `offline_access` in Selected Scopes; Client ID recorded. Submit/Update the provider.

---

## Phase 2 — Enable the device-code flow (one-time, brand-wide)

Authentik ships **no** default device-code flow; the flow is a **brand** setting, not a per-provider toggle.

### 2.1 Create the Stage-Configuration flow
**Flows and Stages > Flows > New Flow > Create.** Set:
- **Name / Title / Slug**: e.g. `default-device-code-flow` / `Device code flow` / `default-device-code-flow`
- **Designation**: **Stage Configuration**
- **Authentication**: **Require authentication**

Add **no stages** — the minimal empty flow is correct. (Doc's verbatim timing note, quoted as-is and not paraphrased: *"This flow is run after the user logs in, and before the user authenticates."* In practice it renders the code-entry/confirmation UI around the brand's normal authentication.)

> Idempotent: if a Stage-Configuration flow already exists for device code, reuse it — don't create a duplicate.

### 2.2 Bind the flow to the active brand → enables device-code grant brand-wide
**System > Brands > *(default/active brand)* > Edit** → set the device-code binding field to the flow from 2.1 → **Update**.
⚠ **Label skew (real, one field):** the device-code setup doc calls this **"Default code flow"**; the Brands reference doc calls it **"Device code flow"**. The model has exactly one field (`flow_device_code`). Set the single device-code binding field regardless of label.

> 🛑 If the brand **already** has a device-code flow bound, do **not** clobber it — that binding is brand-wide and may serve other clients. Reuse it.

✅ **Gate 2:** the active brand has a device-code flow bound. (Discovery advertises `device_authorization_endpoint` **unconditionally** regardless — but the POST to `/application/o/device/` only *completes* because of this binding. Both are required.)

---

## Phase 3 — Verify the discovery document (gate before touching siftd)

```bash
curl -s https://<host>/application/o/<slug>/.well-known/openid-configuration | jq \
  '{issuer, jwks_uri, token_endpoint, device_authorization_endpoint,
    id_token_signing_alg_values_supported, scopes_supported}'
```

✅ **Gate 3 — all must hold:**
- `issuer` **==** `ISSUER_URL` exactly (including trailing slash).
- `id_token_signing_alg_values_supported` contains **`RS256`** (or `ES256`) and **NOT `HS256`**. → HS256 means the **Signing Key did not take** (§1.2); fix before proceeding.
- `token_endpoint` present (global `/application/o/token/`) **and** `device_authorization_endpoint` present (global `/application/o/device/`).
- `jwks_uri` shares the **same scheme + host + port** as `issuer` (per-app path `…/application/o/<slug>/jwks/`). siftd **enforces** this (`auth.py:308`); a mismatch (usually a Caddy header problem, Gate 0) breaks auth.
- `scopes_supported` includes `offline_access` (confirms §1.5 took — it's built dynamically from the provider's selected scope mappings).

Because discovery advertises both endpoints, siftd's `auth.device_authorization_endpoint` / `auth.token_endpoint` **overrides are not needed** on a stock provider.

---

## Phase 4 — Configure the siftd CLIENT and log in (§5.2)

On a client machine:

```bash
siftd config set auth.issuer    https://<host>/application/o/<slug>/
siftd config set auth.client_id <Client ID from §1.6>
# auth.scope defaults to "openid offline_access" — set explicitly only to add custom scopes:
# siftd config set auth.scope "openid offline_access siftd:read siftd:write"

siftd auth login     # device-code: prints user_code + verification_uri; approve in a browser
siftd auth status
```

`siftd auth login` POSTs to the (discovered) device endpoint, polls the token endpoint with `grant_type=urn:ietf:params:oauth:grant-type:device_code` (`error=authorization_pending` until you approve), then stores `access_token` + `refresh_token`.

✅ **Gate 4:** `siftd auth login` completes after browser approval; `siftd auth status` shows an active session **with a refresh token persisted**. If no refresh token: `offline_access` is missing on the provider (§1.5) or in `auth.scope`.
⚠ Verify-on-instance: upstream #11399 reported device-code + `offline_access` *not* returning a refresh token on some releases. If Gate 4 shows no refresh token despite §1.5 + scope being correct, suspect this and check your point release.

---

## Phase 5 — Decode the token; capture iss / aud / exp / alg (LOCAL only)

**Do not paste the token into any web tool.** Decode the access token locally:

```bash
# <ACCESS_TOKEN> = the access token from `siftd auth status` (or its debug output / token store)
python3 - <<'PY'
import base64, json, sys
tok = sys.argv[1] if len(sys.argv)>1 else input("token: ").strip()
h,p,_ = tok.split(".")
pad = lambda s: s + "="*(-len(s)%4)
print("HEADER :", json.dumps(json.loads(base64.urlsafe_b64decode(pad(h))), indent=2))
print("PAYLOAD:", json.dumps(json.loads(base64.urlsafe_b64decode(pad(p))), indent=2))
PY <ACCESS_TOKEN>
```

✅ **Gate 5 — record these exact values:**
- Header `alg` = **`RS256`** (or `ES256`). If `HS256` → signing key didn't take (back to §1.2).
- `iss` — **capture the exact string, including any trailing slash.** ← see the BLOCKER below.
- `aud` — this is what `[serve.auth] audience` must equal. Source says `aud = client_id`; **trust the decoded value, not the assumption.** If `aud` is absent or not the client_id, add an **Audience** property mapping (`Customization > Property Mappings > Scope/Provider Mapping`, *Included Client Audience = this provider*) and re-issue.
- `exp`, `iat` present (siftd requires `exp`; `auth.py:220`).

> 🛑🔴 **BLOCKING — trailing-slash `iss` mismatch.** Authentik's per-provider `iss` is `https://<host>/application/o/<slug>/` **with a trailing slash**. siftd does `issuer = config["issuer"].rstrip("/")` (`auth.py:203`) and passes that to PyJWT, which does **exact string equality** on `iss` — no normalization. So `".../slug" != ".../slug/"` → `InvalidIssuerError` → **every Authentik token is rejected**, and **no config value fixes it** (rstrip strips any slash you add). If the decoded `iss` ends in `/`, treat this as a **code-level blocker at `auth.py:203`**, not a config issue — flag to the human before flipping the server. (The prior OIDC integration likely "worked" against a Keycloak `…/realms/main` issuer with no trailing slash, which never exposed this.)

---

## Phase 6 — Migrate the siftd SERVER: introspection → JWKS (§5.1)

Only after Gates 3, 4, 5 pass. On the server, in `[serve.auth]`:

```toml
[serve.auth]
issuer   = "https://<host>/application/o/<slug>/"   # ISSUER_URL — selects the JWT/JWKS path
audience = "<aud from Gate 5>"                       # siftd default is literally "siftd" — MUST be overridden
# identity_claim defaults to "sub" on the JWT path — set only if you changed Subject mode and want a different claim
required_scopes = ["siftd:read"]                     # if you minted custom scopes
write_scopes    = ["siftd:write"]
# REMOVE introspection_url entirely — its presence makes siftd pick the introspection branch (auth.py:163)
```

Mode is chosen by **key presence** (`auth.py:159-166`): `issuer` present → JWT/JWKS; else `introspection_url` → introspection. You must **delete `introspection_url`** or it loses the race only if `issuer` is absent — keep it removed to avoid confusion. Restart `siftd serve` (e.g. `sudo systemctl restart siftd-serve`).

### End-to-end verification

```bash
# From the client that ran `siftd auth login`:
siftd auth status                       # active, token valid

# Direct bearer check against the live server (token from auth store):
curl -i -H "Authorization: Bearer <ACCESS_TOKEN>" https://<host>/api/v1/stats
#   → 200 with JSON stats  = JWKS validation works
#   → 401 "Invalid token"  = iss/aud/alg mismatch — recheck Gate 5 (esp. trailing-slash iss)
#   → 401 "Missing bearer token" = no header (expected without one)
```

✅ **Gate 6:** `GET /api/v1/stats` with the device-code token returns **200**. `journalctl -u siftd-serve` (or server log) shows JWKS fetched once and no auth errors. The introspection endpoint is no longer called.

---

## Phase 7 — Two-refresh test (Authentik rotates + revokes by default)

Authentik's `refresh_token_threshold` defaults to **`seconds=0`** → *"token will always be renewed"*: every refresh issues a **new** refresh token and **revokes the old one**. If siftd reuses the original refresh token after the first refresh, the **second** refresh fails (old token revoked) and re-prompts.

```bash
# Force two consecutive refreshes: wait past access_token_validity (default hours=1; lower it on the
# provider's "Access token validity" to e.g. minutes=2 for the test), then:
siftd auth status      # triggers refresh #1 — should succeed silently
# wait past validity again
siftd auth status      # triggers refresh #2 — MUST also succeed silently
```

✅ **Gate 7:** two consecutive refreshes succeed **without** a re-login prompt.
- If refresh #2 re-prompts: either siftd isn't persisting the **rotated** refresh token (siftd-side bug), **or** set the provider's **`refresh_token_threshold` > 0** (e.g. `days=1`) so the same refresh token is reused until near expiry — this sidesteps rotation churn. (Note `expires_in` on a refresh grant reflects **access**-token validity, not refresh validity — by design.)

> Reset `Access token validity` back to your real value (e.g. `hours=1`) after the test. Consider raising `Refresh token validity` (default `days=30`) to e.g. `days=90` for long-lived CLI sessions.

---

## §R — Rollback (revert to introspection if JWKS validation fails)

If Gate 6 cannot pass (e.g. the trailing-slash `iss` blocker, or persistent `Invalid token`):

1. **Server** `[serve.auth]`: remove `issuer` and `audience`; restore `introspection_url = "<the original introspection endpoint>"` (and `client_id`/`client_secret` if the introspection call used them). Restart `siftd serve`.
2. Re-verify: `curl -H "Authorization: Bearer <old-style token>" https://<host>/api/v1/stats` → 200 via introspection.
3. ⚠ **Provider caveat:** you flipped the provider to **Public** in §1.1. Introspection on a *public* provider had bug #11616 (introspection wrongly validating `client_secret`). If the restored introspection path fails on the now-Public provider, revert **Client type → Confidential** as well (🛑 only if Gate 0 confirmed this provider isn't relied on as Public elsewhere) and re-test introspection.
4. The device-code brand flow (Phase 2) is harmless to leave bound; the `offline_access` scope is harmless to leave selected. Roll those back only if required.

Rollback is config-only on the server side plus an optional client-type revert; no data migration is involved.

---

## Quick contract crosswalk (Authentik change → siftd config it satisfies)

| Authentik change | siftd config satisfied |
|---|---|
| Client type = Public (§1.1) | client sends `auth.client_id` with no secret |
| Signing Key = RSA/EC cert (§1.2) | server validates `RS256`/`ES256` JWT (`auth.py:217`) |
| Encryption Key empty (§1.3) | token is a plain signed JWT, not JWE |
| Subject mode (§1.4) | `[serve.auth] identity_claim` (default `sub`) → owner |
| `offline_access` selected (§1.5) | refresh token for `auth.scope` auto-refresh |
| Client ID read-back (§1.6) | `auth.client_id` + `[serve.auth] audience` (confirm via Gate 5) |
| Stage-Config flow + brand bind (Phase 2) | `siftd auth login` device-code grant runs |
| `ISSUER_URL` (Gate 0/3) | `auth.issuer` (client) + `[serve.auth] issuer` (server); JWKS-origin check (`auth.py:308`) |
| Remove introspection (Phase 6) | server picks JWT/JWKS branch (`auth.py:161` vs `:163`) |
