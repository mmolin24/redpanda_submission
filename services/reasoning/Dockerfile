FROM ghcr.io/astral-sh/uv:0.11.11@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a AS uv

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

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
