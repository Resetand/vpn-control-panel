FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Only manifests — no src yet.  This layer is reused whenever only source
# files change (pyproject.toml / uv.lock changes still bust the dep layer).
COPY pyproject.toml uv.lock README.md ./

# ── runtime image ─────────────────────────────────────────────────────────────
FROM base AS app-deps

RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*

# Venv outside /app so volume mounts never shadow it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Heavy layer: only re-runs when pyproject.toml / uv.lock change.
RUN uv sync --no-dev --no-install-project --frozen

FROM app-deps AS app

# Lightweight layer: registers the package itself (deps already cached above).
COPY src ./src
RUN uv sync --no-dev --frozen

EXPOSE 8080

CMD ["vpn-control-plane"]

# ── dev / CI image ────────────────────────────────────────────────────────────
FROM base AS dev

RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*

# Venv outside /app so the .:/app volume mount never shadows installed packages.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:$PATH

# All deps including dev extras, without the project itself.
# Source is provided at runtime via the volume mount (.:/app), so no COPY src.
RUN uv sync --frozen --no-install-project --extra dev

# Make the mounted src/ importable without a package install.
ENV PYTHONPATH=/app/src

CMD ["pytest"]
