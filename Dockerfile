# syntax=docker/dockerfile:1
#
# Production image for `siftd serve` (the [serve] extra).
#
# Multi-stage: the builder resolves and installs siftd + its serve
# dependencies into an isolated venv; the runtime stage copies only that
# venv, so build tooling (uv, compilers) never ships in the final image.
#
# The container reads its config from $XDG_CONFIG_HOME/siftd/config.toml.
# XDG_CONFIG_HOME is pinned to /etc below, so the operative path is
# /etc/siftd/config.toml — mount it read-only at runtime. The conversation
# DB lives under /var/lib/siftd (mount a volume there). This mirrors the
# FHS layout documented in docs/ops/homelab.md §3.
#
# Smoke topology (smoke/homelab/Dockerfile.serve) builds from the local
# source the same way; this is its productionized sibling (non-root,
# multi-stage, healthcheck, OCI labels).

# ---- builder ---------------------------------------------------------------
FROM python:3.13-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /src
# pyproject + README first so the metadata layer caches independently of src.
COPY pyproject.toml README.md ./
COPY src ./src
# plugin/ is a forced-include wheel target (see [tool.hatch.build.targets.wheel
# .force-include] in pyproject.toml) — hatchling needs it present at build time.
COPY plugin ./plugin

# Install siftd + the serve extra into a relocatable venv. Same base image in
# both stages, so the venv's interpreter shebangs resolve in the runtime stage.
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache '.[serve]'

# ---- runtime ---------------------------------------------------------------
FROM python:3.13-slim AS runtime

# curl: container healthcheck against /api/v1/health.
# ca-certificates: outbound TLS for OIDC JWKS / RFC 7662 introspection calls
# to the IdP (introspection mode POSTs to the IdP's HTTPS endpoint per request).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 -s /usr/sbin/nologin siftd

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    XDG_CONFIG_HOME=/etc \
    PYTHONUNBUFFERED=1

# Config dir (read-only mount target) and DB dir (writable volume target),
# owned by the non-root runtime user.
RUN mkdir -p /etc/siftd /var/lib/siftd \
    && chown -R siftd:siftd /var/lib/siftd

USER siftd
WORKDIR /var/lib/siftd
EXPOSE 8484

# /api/v1/health is auth-exempt, so the check needs no token.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8484/api/v1/health || exit 1

LABEL org.opencontainers.image.title="siftd-serve" \
      org.opencontainers.image.description="siftd HTTP server (personal LLM usage analytics)" \
      org.opencontainers.image.source="https://github.com/kgruel/siftd" \
      org.opencontainers.image.licenses="MIT"

# --host 0.0.0.0 overrides the config's loopback bind so a remote reverse
# proxy (Caddy on a separate host) can reach the daemon. Auth still applies —
# siftd validates every request's bearer token regardless of bind address.
CMD ["siftd", "serve", "--host", "0.0.0.0", "--port", "8484"]
