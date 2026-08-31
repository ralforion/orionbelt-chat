# syntax=docker/dockerfile:1

# ── OrionBelt Chat container image ─────────────────────────────────────────
# Builds the Chainlit + Pydantic AI chat client with uv for fast, reproducible
# dependency installs.
#
# Build:  docker build -t orionbelt-chat .
# Run:    docker run --rm -p 8080:8080 --env-file .env orionbelt-chat
# ───────────────────────────────────────────────────────────────────────────

FROM python:3.13-slim AS base

# uv binary, pulled from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# uv configuration:
#   - compile bytecode for faster container startup
#   - copy (not symlink) packages so the venv is self-contained
#   - install into a project-local .venv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# ── Dependency layer ──────────────────────────────────────────────
# Copy only the dependency manifests first so this layer is cached across
# source-only changes. uv.lock is committed for reproducible installs.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# ── Application layer ─────────────────────────────────────────────
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── Attribution layer ─────────────────────────────────────────────
# The image ships binary copies of every dependency in /app/.venv, which is
# redistribution: Apache-2.0 §4, the MIT/BSD/ISC notice clauses and MPL-2.0
# §3.2 all require their notices to travel with those copies. Most wheels carry
# a LICENSE in their .dist-info and uv copies it in, but ~39 do not, so the
# notice has to account for those too.
#
# Copied in rather than generated here. The notice is committed and reviewed in
# the same diff as the dependency change that altered it, and CI proves the
# committed copy matches the locked set — so building it again inside the image
# would only create a second answer that could disagree with the reviewed one.
COPY THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md

# Chainlit's app root: where public/, chainlit.md, .chainlit/config.toml are
# seeded from the package and where runtime state (.files/) is written.
# Pinned under /app rather than the home directory so it is easy to mount.
ENV CHAINLIT_APP_ROOT=/app/runtime

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runtime \
    && chown -R appuser:appuser /app
USER appuser

# Chainlit serves on 8080 (matches the documented local URL).
EXPOSE 8080

# The console script seeds the app root and then runs Chainlit against the
# packaged app (see orionbelt_chat/cli.py); Chainlit's own flags pass through.
# --host 0.0.0.0 makes the server reachable from outside the container;
# --headless skips the browser-open attempt that has no display in a container.
CMD ["orionbelt-chat", "--host", "0.0.0.0", "--port", "8080", "--headless"]
