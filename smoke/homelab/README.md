# siftd smoke harness — docker-compose homelab stack

Local dress-rehearsal for the homelab topology: Caddy (TLS termination) →
siftd-serve (HTTP, bridge-only) ← mock-oidc (OIDC IdP, bridge-only).

Mirrors `docs/ops/homelab.md` with the public IdP and DNS replaced by
containers. Smoke-tests here transfer directly to real-hardware deploys.

## Services

| Service | Image / build | Published ports | Network |
|---|---|---|---|
| `caddy` | `caddy:2-alpine` | 443, 80 → host | `smoke-net` |
| `siftd-serve` | built from `Dockerfile.serve` | bridge only | `smoke-net` |
| `mock-oidc` | `ghcr.io/navikt/mock-oauth2-server:2.1.10` | bridge only | `smoke-net` |

## Bring up by hand

```bash
cd smoke/homelab

# First run: builds siftd-serve image (~2 min).
docker compose up -d --wait

# Verify all three are healthy.
docker compose ps
```

`--wait` blocks until all healthchecks pass (or times out). Expect ~30–60 s
on first boot due to siftd-serve startup and mock-oidc JVM warmup.

**Build context note**: `Dockerfile.serve.dockerignore` (per-Dockerfile ignore file)
is only honored by Docker BuildKit. Without BuildKit, the full repo root is sent as
build context (~40 MB). To enable: install the buildx plugin and set
`DOCKER_BUILDKIT=1` before running `docker compose build`, or use Docker Desktop
which ships buildx by default.

## Get a token

mock-oidc is bridge-only and its image is distroless (no shell, wget, or curl).
Reach the token endpoint through `siftd-serve`, which is on the same bridge
network and has `curl` installed:

```bash
TOKEN=$(docker compose exec siftd-serve \
  curl -sf \
  -X POST 'http://mock-oidc:8080/default/token' \
  -d 'grant_type=client_credentials&client_id=smoke-client&client_secret=unused' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

echo $TOKEN
```

The token carries `sub=smoke-client`, `aud=siftd-smoke`, and
`scope=siftd:read siftd:write` (configured in `mock-oidc.json`).

Minting via `siftd-serve` matters for the issuer URL: mock-oauth2-server derives
`iss` from the `Host` header it receives. Requests from inside `smoke-net` use the
Docker DNS name `mock-oidc:8080`, so `iss=http://mock-oidc:8080/default` — which is
what `siftd-serve.config.toml` expects. See the "Issuer URL" caveat for details.

## Hit the server

`/api/v1/health` is auth-exempt; any other route requires a bearer token.

```bash
# Health check — no token needed. Use --resolve to skip /etc/hosts editing:
curl -sk --resolve siftd.smoke.local:443:127.0.0.1 \
     https://siftd.smoke.local/api/v1/health | python3 -m json.tool

# Stats — requires token.
curl -sk --resolve siftd.smoke.local:443:127.0.0.1 \
     -H "Authorization: Bearer $TOKEN" \
     https://siftd.smoke.local/api/v1/stats | python3 -m json.tool

# Without a token — expect 401 Missing bearer token.
curl -sk --resolve siftd.smoke.local:443:127.0.0.1 \
     https://siftd.smoke.local/api/v1/stats
```

(The `-k` / `-s` flags bypass TLS verification for Caddy's internal CA.
To verify properly, trust the CA root — see "Caveats" below.)

## Verifying the OIDC stack end-to-end

```bash
# Get a token — exec through siftd-serve so the Host header is mock-oidc:8080
# (distroless mock-oidc has no shell or curl; siftd-serve is on the same bridge).
TOKEN=$(docker compose exec siftd-serve \
  curl -sf \
  -X POST 'http://mock-oidc:8080/default/token' \
  -d 'grant_type=client_credentials&client_id=smoke-client&client_secret=unused' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

# Confirm OIDC discovery issuer matches siftd-serve config:
docker compose exec siftd-serve curl -sf \
  http://mock-oidc:8080/default/.well-known/openid-configuration \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["issuer"])'
# → http://mock-oidc:8080/default

# Hit Caddy with the token (using --resolve to avoid /etc/hosts modification):
curl -sk --resolve siftd.smoke.local:443:127.0.0.1 \
  -H "Authorization: Bearer $TOKEN" \
  https://siftd.smoke.local/api/v1/stats | python3 -m json.tool
```

### mock-oidc is distroless

The `ghcr.io/navikt/mock-oauth2-server` 2.x image is JIB-built with a
distroless base — it contains only the JVM and no shell, curl, or wget. This
has two implications:

1. **Healthcheck**: uses a bind-mounted Java single-file helper
   (`HealthCheck.java`) compiled and run in-process via Java 11's
   source launcher (JEP 330). The file is mounted at `/tmp/HealthCheck.java`.
   The filename matches the public class name per Java convention; JEP 330
   source-file mode enables in-place compile-and-run without `javac`.
2. **`docker compose exec mock-oidc curl ...` won't work.** Use
   `docker compose exec siftd-serve curl ... http://mock-oidc:8080/...`
   to reach mock-oidc through the bridge network instead.

## Caveats

### `/etc/hosts` mapping

`siftd.smoke.local` must resolve to `127.0.0.1` on the host for the curls
above to reach Caddy. Add one line (root required):

```
127.0.0.1  siftd.smoke.local
```

The ST-2 harness driver manages this automatically. For manual testing,
add and remove by hand. To avoid leaving a stale entry:

```bash
# Add
echo '127.0.0.1  siftd.smoke.local' | sudo tee -a /etc/hosts

# Remove when done
sudo sed -i '' '/siftd\.smoke\.local/d' /etc/hosts   # macOS
sudo sed -i '/siftd\.smoke\.local/d' /etc/hosts       # Linux
```

### Caddy internal CA

Caddy generates a local CA on first boot (stored in the `caddy-data` named
volume under `/data/caddy/pki/authorities/local/`). Extract the root cert to
verify TLS properly:

```bash
docker compose exec caddy \
  cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt

# Trust on macOS (adds to System keychain — requires password):
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain caddy-root.crt

# Or pass to curl directly:
curl --cacert caddy-root.crt https://siftd.smoke.local/api/v1/health
```

`down -v` destroys the `caddy-data` volume, regenerating the CA on next
`up`. The CA root cert will change — re-extract and re-trust if needed.

### mock-oidc bridge isolation

The token-endpoint (`http://mock-oidc:8080/default/token`) is only reachable
from inside the `smoke-net` bridge. Use `docker compose exec siftd-serve curl ...`
as shown in "Get a token" above, or start a standalone container on the same network:

```bash
docker run --rm --network homelab_smoke-net curlimages/curl:latest \
  -X POST 'http://mock-oidc:8080/default/token' \
  -d 'grant_type=client_credentials&client_id=smoke-client&client_secret=unused'
```

### Issuer URL

siftd-serve validates that the JWT `iss` claim matches `issuer` in
`siftd-serve.config.toml` exactly. mock-oauth2-server derives `iss` from
the `Host` header it receives — siftd-serve reaches it as
`http://mock-oidc:8080` (Docker DNS), so `iss` in every token is
`http://mock-oidc:8080/default`. Do not change the issuer URL in the config
without also changing how mock-oidc is reached.

## Teardown

```bash
# Stop and remove containers; preserve volumes.
docker compose down

# Full reset — removes volumes (caddy CA + siftd DB).
docker compose down -v
```

The ST-2 driver always uses `down -v` to start each run from a clean slate.

## Known TODOs

- **Authorization-header scrubbing in Caddy access logs** — the `format filter`
  log encoder that can redact `request>headers>Authorization` requires the
  `caddy-filter` module, which is not bundled in `caddy:2-alpine`. Until the
  harness switches to a custom Caddy build (or the module ships in stock
  Alpine), avoid enabling full header logging in the `log` block. The current
  Caddyfile logs structured JSON but does not log individual headers. See the
  TODO comment in `Caddyfile` for the target syntax once the module is available.
