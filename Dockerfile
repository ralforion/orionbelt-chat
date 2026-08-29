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
# a LICENSE in their .dist-info and uv copies it in, but ~39 do not, so collect
# the lot into one file the recipient can actually find.
RUN version=$(grep -m1 '^version = ' pyproject.toml | sed -E 's/^version = "([^"]+)"/\1/') \
    && python scripts/gen_third_party_licenses.py \
        --venv /app/.venv \
        --version "$version" \
        --overrides licenses \
        --fail-on-missing-notice \
        -o /app/THIRD_PARTY_LICENSES.md

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Chainlit serves on 8080 (matches the documented local URL).
EXPOSE 8080

# --host 0.0.0.0 makes the server reachable from outside the container;
# --headless skips the browser-open attempt that has no display in a container.
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8080", "--headless"]
