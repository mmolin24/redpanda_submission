FROM ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 AS uv

FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/

WORKDIR /app/services/reasoning
COPY services/reasoning/pyproject.toml services/reasoning/uv.lock ./
RUN uv sync --frozen --no-dev --extra runtime --no-install-project

COPY services/reasoning/src ./src
RUN uv sync --frozen --no-dev --extra runtime

USER 65532:65532
ENTRYPOINT ["/app/services/reasoning/.venv/bin/pypi-reasoning-worker"]
