#!/usr/bin/env bash
# smoke-homelab.sh
# DESC: End-to-end docker-compose homelab smoke harness driver
# Usage: ./dev smoke-homelab
# Dependencies: docker, uv, curl, jq

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/log.sh
source "$SCRIPT_DIR/lib/log.sh"

readonly STACK_DIR="$REPO_ROOT/smoke/homelab"
readonly ARTIFACT_DIR="/tmp/siftd-smoke-homelab"
readonly CLIENT_HOME="$ARTIFACT_DIR/client-home"
readonly CADDY_ROOT_CA="$ARTIFACT_DIR/caddy-root.crt"
readonly SUMMARY="$ARTIFACT_DIR/SUMMARY.md"
readonly SERVER_URL="https://localhost"

COMPOSE_UP=0
PROBE_RESULTS=()

cleanup() {
    local rc=$?
    log_info "Cleanup..."
    if [ "$COMPOSE_UP" = 1 ]; then
        # Capture server logs before teardown for debugging
        (cd "$STACK_DIR" && docker compose logs --no-color siftd-serve 2>&1 | tail -200 > "$ARTIFACT_DIR/siftd-serve.log") || true
        (cd "$STACK_DIR" && docker compose logs --no-color caddy 2>&1 | tail -100 > "$ARTIFACT_DIR/caddy.log") || true
        (cd "$STACK_DIR" && docker compose down -v --remove-orphans 2>&1 | tail -3) || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

record_probe() {
    local n="$1" name="$2" status="$3" reason="$4"
    PROBE_RESULTS+=("$n|$name|$status|$reason")
    if [ "$status" = "PASS" ]; then
        log_success "P$n $name — $reason"
    else
        log_warn "P$n $name — $reason"
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
log_info "Pre-flight checks..."
command -v docker >/dev/null || { log_error "docker not found"; exit 1; }
command -v uv >/dev/null || { log_error "uv not found"; exit 1; }
command -v curl >/dev/null || { log_error "curl not found"; exit 1; }
command -v jq >/dev/null || { log_error "jq not found (brew install jq)"; exit 1; }
test -f "$REPO_ROOT/pyproject.toml" || { log_error "not in siftd repo root"; exit 1; }
test -d "$STACK_DIR" || { log_error "smoke/homelab/ missing"; exit 1; }
export DOCKER_BUILDKIT=1

# Ensure host venv has the [serve] extra (httpx) for `siftd db push`. Worker
# worktrees default to `uv sync --extra dev` only; this catches that case
# without surprising the user. Idempotent / fast when already installed.
(cd "$REPO_ROOT" && uv sync --extra serve --extra dev --quiet 2>&1 | tail -3)

# ---------------------------------------------------------------------------
# Artifact dir + stack startup
# ---------------------------------------------------------------------------
rm -rf "$ARTIFACT_DIR"
mkdir -p "$ARTIFACT_DIR" "$CLIENT_HOME/config/siftd" "$CLIENT_HOME/data/siftd"

log_info "Bringing up docker-compose stack (build to pick up latest source)..."
(cd "$STACK_DIR" && docker compose up -d --build --wait 2>&1 | tail -8)
COMPOSE_UP=1
log_success "Stack healthy"

# ---------------------------------------------------------------------------
# Trust + hosts
# ---------------------------------------------------------------------------
log_info "Extracting Caddy root CA..."
(cd "$STACK_DIR" && docker compose exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$CADDY_ROOT_CA")
test -s "$CADDY_ROOT_CA" || { log_error "Caddy CA empty"; exit 1; }
export SSL_CERT_FILE="$CADDY_ROOT_CA"
export REQUESTS_CA_BUNDLE="$CADDY_ROOT_CA"

# Note: Caddyfile serves on both `siftd.smoke.local` and `localhost`, so the
# harness uses `https://localhost` to avoid needing /etc/hosts entries (and
# the sudo prompt that requires).

# ---------------------------------------------------------------------------
# Mint OIDC token via mock-oauth2-server
# ---------------------------------------------------------------------------
log_info "Minting OIDC token..."
TOKEN_RESPONSE=$(cd "$STACK_DIR" && docker compose exec -T siftd-serve curl -sS \
    -X POST http://mock-oidc:8080/default/token \
    -u 'smoke-client:smoke-secret' \
    -d 'grant_type=client_credentials' \
    -d 'scope=siftd:read siftd:write')
TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r .access_token)
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    log_error "Token mint failed: $TOKEN_RESPONSE"
    exit 1
fi
log_success "Token minted ($(echo "$TOKEN" | head -c 20)...)"
export SIFTD_SERVE_TOKEN="$TOKEN"
export SIFTD_SERVE_DELEGATION_TOKEN="$TOKEN"

# ---------------------------------------------------------------------------
# Client config
# ---------------------------------------------------------------------------
export XDG_CONFIG_HOME="$CLIENT_HOME/config"
export XDG_DATA_HOME="$CLIENT_HOME/data"
export XDG_STATE_HOME="$CLIENT_HOME/state"
mkdir -p "$XDG_STATE_HOME"

cat > "$CLIENT_HOME/config/siftd/config.toml" <<EOF
[serve]
url = "$SERVER_URL"
delegate = true

[serve.auth]
delegation_token = "$TOKEN"

[sync.remotes.homelab]
path = "$SERVER_URL"

[sync.remotes.homelab.auth]
type = "bearer"
token = "$TOKEN"
EOF

# ---------------------------------------------------------------------------
# Initialize server-side empty schema (siftd serve doesn't auto-create)
# ---------------------------------------------------------------------------
log_info "Initializing server-side empty DB schema..."
(cd "$STACK_DIR" && docker compose exec -T siftd-serve python -c "
from pathlib import Path
from siftd.storage.sqlite import create_database
p = Path('/var/lib/siftd/siftd.db')
if not p.exists():
    create_database(p).close()
    print(f'Created {p}')
else:
    print(f'Exists {p}')
")

# ---------------------------------------------------------------------------
# Build probe DBs
# ---------------------------------------------------------------------------
log_info "Building probe databases..."
uv run python "$SCRIPT_DIR/_smoke_homelab_fixture.py" "$ARTIFACT_DIR/fixtures" 2>&1 | sed 's/^/  /'

# ---------------------------------------------------------------------------
# Probe helper
# ---------------------------------------------------------------------------
SIFTD() { uv run --frozen siftd "$@"; }

run_probe_capture() {
    local probe_log="$1"
    shift
    : > "$probe_log"
    echo "# Probe: $*" >> "$probe_log"
    echo "" >> "$probe_log"
    echo '```' >> "$probe_log"
    set +e
    "$@" >> "$probe_log" 2>&1
    local rc=$?
    set -e
    echo '```' >> "$probe_log"
    echo "" >> "$probe_log"
    echo "exit: $rc" >> "$probe_log"
    return $rc
}

# ---------------------------------------------------------------------------
# P1 — push large DB (>10MB) — expected FAIL pre-#7
# ---------------------------------------------------------------------------
log_info "P1: push large DB (expecting 413 pre-#7)..."
cp "$ARTIFACT_DIR/fixtures/large.db" "$CLIENT_HOME/data/siftd/siftd.db"
LARGE_SIZE_MB=$(stat -f%z "$ARTIFACT_DIR/fixtures/large.db" 2>/dev/null || stat -c%s "$ARTIFACT_DIR/fixtures/large.db")
LARGE_SIZE_MB=$((LARGE_SIZE_MB / 1024 / 1024))
# All push probes use --all because cp'ing a fixture DB over siftd.db does NOT
# reset the per-remote last_push receipt (stored in client config, separate from
# the DB). Without --all, P3 sees fixture.db's 2024-dated convs as predating
# P2's last_push wall-clock timestamp and pushes nothing.
if run_probe_capture "$ARTIFACT_DIR/probe-01-push-large.md" \
    cp "$ARTIFACT_DIR/fixtures/large.db" "$CLIENT_HOME/data/siftd/siftd.db" \
    && run_probe_capture "$ARTIFACT_DIR/probe-01-push-large.md" \
    env SIFTD_DB="$CLIENT_HOME/data/siftd/siftd.db" \
    uv run --frozen siftd db push homelab --all; then
    record_probe 1 "push large (${LARGE_SIZE_MB}MB)" PASS "succeeded — #7 may be fixed"
else
    record_probe 1 "push large (${LARGE_SIZE_MB}MB)" FAIL "rejected — expected pre-#7 (Litestar 10MB cap)"
fi

# ---------------------------------------------------------------------------
# P2 — push small DB (baseline)
# ---------------------------------------------------------------------------
log_info "P2: push small DB (baseline)..."
cp "$ARTIFACT_DIR/fixtures/small.db" "$CLIENT_HOME/data/siftd/siftd.db"
if run_probe_capture "$ARTIFACT_DIR/probe-02-push-small.md" \
    env SIFTD_DB="$CLIENT_HOME/data/siftd/siftd.db" \
    uv run --frozen siftd db push homelab --all; then
    record_probe 2 "push small" PASS "succeeded"
else
    record_probe 2 "push small" FAIL "unexpected — baseline push should work"
fi

# ---------------------------------------------------------------------------
# P3 — push fixture DB (anchor phrases), then exercise reads
# ---------------------------------------------------------------------------
log_info "P3: push fixture DB (anchor phrases)..."
cp "$ARTIFACT_DIR/fixtures/fixture.db" "$CLIENT_HOME/data/siftd/siftd.db"
if run_probe_capture "$ARTIFACT_DIR/probe-03-push-fixture.md" \
    env SIFTD_DB="$CLIENT_HOME/data/siftd/siftd.db" \
    uv run --frozen siftd db push homelab --all; then
    # Verify the push actually moved data — exit 0 with "Nothing new" is a false PASS
    if grep -qi "nothing new" "$ARTIFACT_DIR/probe-03-push-fixture.md"; then
        record_probe 3 "push fixture" FAIL "client reported 'Nothing new' — fixture timestamps may not advance last_push"
    else
        record_probe 3 "push fixture" PASS "succeeded"
    fi
else
    record_probe 3 "push fixture" FAIL "unexpected — fixture push failed"
fi

# Replace local DB with empty schema-only DB so reads delegate to server
# (siftd query requires a local DB to open before deciding to delegate)
uv run python -c "
from siftd.storage.sqlite import create_database
from pathlib import Path
p = Path('$CLIENT_HOME/data/siftd/siftd.db')
p.unlink(missing_ok=True)
create_database(p).close()
"

# ---------------------------------------------------------------------------
# P4 — query --recent (baseline delegated read)
# ---------------------------------------------------------------------------
log_info "P4: query (list recent — baseline read)..."
if run_probe_capture "$ARTIFACT_DIR/probe-04-query-list.md" \
    uv run --frozen siftd query -n 5 && \
   grep -qE "^\| [0-9A-Z]{12}" "$ARTIFACT_DIR/probe-04-query-list.md"; then
    record_probe 4 "query (list)" PASS "returns conversations"
else
    record_probe 4 "query (list)" FAIL "no conversations returned"
fi

# Capture a real ULID from P4 output for later probes. siftd query expects a
# ULID prefix; the c001-style external_ids used by the fixture are NOT what
# `query <id>` resolves against. P5/P6 must consume this resolved prefix.
FIRST_ID=$(grep -oE "^\| [0-9A-Z]{12}" "$ARTIFACT_DIR/probe-04-query-list.md" 2>/dev/null | head -1 | awk '{print $2}')
log_info "  resolved first conversation ULID prefix: ${FIRST_ID:-<none>}"

if [ -z "$FIRST_ID" ]; then
    log_warn "FIRST_ID empty — P5/P6/P7 will be skipped (P4 must succeed first)"
fi

# ---------------------------------------------------------------------------
# P5 — query --around (anchor phrase) — FAIL pre-#9 (FTS not rebuilt on push)
# Anchor phrase lives in c001 turn 3 per _smoke_homelab_fixture.py. The
# fixture builder writes 20 conversations; we use the first ULID resolved
# from P4 and search for the anchor phrase, which should land in ONE of the
# server's conversations if FTS is built correctly.
# ---------------------------------------------------------------------------
log_info "P5: query --around 'smoke-test-anchor-alpha' (expecting empty pre-#9)..."
if [ -n "$FIRST_ID" ] && run_probe_capture "$ARTIFACT_DIR/probe-05-around.md" \
    uv run --frozen siftd query "$FIRST_ID" --around "smoke-test-anchor-alpha" && \
   grep -qi "smoke-test-anchor-alpha\|turn 3\|turn-3" "$ARTIFACT_DIR/probe-05-around.md"; then
    record_probe 5 "query --around" PASS "anchor phrase found — #9 may be fixed"
else
    record_probe 5 "query --around" FAIL "phrase not found — expected pre-#9 (FTS rebuild gap)"
fi

# ---------------------------------------------------------------------------
# P6 — bad --around phrase — FAIL pre-#10 (silent local fallback on server 4xx)
# ---------------------------------------------------------------------------
log_info "P6: query --around 'no-such-phrase' (expecting server 4xx pre-#10)..."
PROBE_LOG="$ARTIFACT_DIR/probe-06-around-notfound.md"
set +e
uv run --frozen siftd query "${FIRST_ID:-MISSING}" --around "phrase-that-does-not-exist-anywhere" > "$PROBE_LOG" 2>&1
RC=$?
set -e
# Expected: server returns 4xx and CLI surfaces it (post-#10).
# Pre-#10: CLI silently falls back to local and returns success/empty.
if [ "$RC" -ne 0 ] && grep -qi "not found\|4[0-9][0-9]\|error" "$PROBE_LOG"; then
    record_probe 6 "query --around (not found)" PASS "server 4xx surfaced — #10 may be fixed"
else
    record_probe 6 "query --around (not found)" FAIL "silent fallback or unclear error — expected pre-#10"
fi

# ---------------------------------------------------------------------------
# P7 — search "smoke-test-anchor-bravo" with --mode fts — #8 (no-embed delegation)
# ---------------------------------------------------------------------------
log_info "P7: search 'smoke-test-anchor-bravo' --mode fts (#8 territory)..."
if run_probe_capture "$ARTIFACT_DIR/probe-07-search-fts.md" \
    uv run --frozen siftd search "smoke-test-anchor-bravo" --mode fts && \
   grep -q "smoke-test-anchor-bravo\|c007" "$ARTIFACT_DIR/probe-07-search-fts.md"; then
    record_probe 7 "search --mode fts" PASS "results returned"
else
    record_probe 7 "search --mode fts" FAIL "no results — likely #8 (short-circuit) or #9 (no FTS)"
fi

# ---------------------------------------------------------------------------
# P8 — tag write delegation
# ---------------------------------------------------------------------------
log_info "P8: tag write delegation..."
if [ -n "${FIRST_ID:-}" ] && run_probe_capture "$ARTIFACT_DIR/probe-08-tag.md" \
    uv run --frozen siftd tag "$FIRST_ID" smoke-tag; then
    record_probe 8 "tag write" PASS "tag write succeeded"
else
    record_probe 8 "tag write" FAIL "tag write failed (id=${FIRST_ID:-unset})"
fi

# ---------------------------------------------------------------------------
# SUMMARY.md
# ---------------------------------------------------------------------------
{
    echo "# Smoke harness run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
    echo "Source: \`main@$(cd "$REPO_ROOT" && git rev-parse --short HEAD)\`"
    echo "Binary: \`$(uv run --frozen siftd --version 2>&1 | head -1)\`"
    echo ""
    echo "## Probe results"
    echo ""
    echo "| Probe | Name | Status | Notes |"
    echo "|-------|------|--------|-------|"
    for entry in "${PROBE_RESULTS[@]}"; do
        IFS='|' read -r n name status reason <<< "$entry"
        echo "| P$n | $name | $status | $reason |"
    done
    echo ""
    echo "## Known-bug map"
    echo ""
    echo "- **#7 (P1)** large body cap — flips PASS when ST-3a lands"
    echo "- **#8 (P7)** no-embed FTS fallback gate — flips PASS when ST-4a lands"
    echo "- **#9 (P5)** FTS rebuild on push — flips PASS when ST-3b lands"
    echo "- **#10 (P6)** silent server-4xx fallback — flips PASS when ST-4b lands"
    echo ""
    echo "## Artifacts"
    echo ""
    echo "- Caddy root CA: \`$CADDY_ROOT_CA\`"
    echo "- Per-probe logs: \`$ARTIFACT_DIR/probe-*.md\`"
    echo "- Client home: \`$CLIENT_HOME\`"
} > "$SUMMARY"

log_info ""
log_info "Summary: $SUMMARY"
cat "$SUMMARY" | tail -25
