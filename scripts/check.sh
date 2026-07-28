#!/bin/sh
set -eu

export UV_CACHE_DIR="${UV_CACHE_DIR:-.uv-cache}"

docker compose --env-file /dev/null -f docker-compose.yml config --quiet
docker compose --env-file /dev/null config --quiet

uv sync --project services/reasoning --extra runtime --extra test --frozen
uv sync --project services/api --extra test --frozen
uv run --project tools/python-quality --frozen ruff format --check .
uv run --project tools/python-quality --frozen ruff check .
uv run --project tools/python-quality --frozen pyright \
  --pythonpath services/reasoning/.venv/bin/python \
  services/reasoning/src scripts infra/lifecycle.py tests/reasoning tests/contracts tests/schema_catalog.py
uv run --project tools/python-quality --frozen pyright \
  --pythonpath tools/python-quality/.venv/bin/python tests/platform
uv run --project tools/python-quality --frozen pyright \
  --pythonpath services/api/.venv/bin/python services/api/app tests/product/api

uv run --project services/reasoning --extra test --frozen pytest tests/contracts
uv run --project tools/python-quality --frozen python -m unittest discover \
  -s tests/platform -p 'test_*.py'
./infra/connect/lint-configs.sh
sh infra/prometheus/check-config.sh
sh infra/connect/test-source-metrics.sh
uv run --project services/reasoning --extra test --frozen pytest tests/reasoning
uv run --project services/api --extra test --frozen pytest tests/product/api

pnpm --dir web format:check
pnpm --dir web lint
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
