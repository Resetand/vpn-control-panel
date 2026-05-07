FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

FROM base AS app

RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir .

EXPOSE 8080

CMD ["vpn-control-plane"]

FROM base AS dev

RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*
COPY tests ./tests
RUN python -m pip install --no-cache-dir -e '.[dev]'

CMD ["pytest"]