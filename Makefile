.PHONY: help format check smoke up-fixture stop-safe

help:
	@echo "make format       Format Python code"
	@echo "make check        Run every local assessment check"
	@echo "make smoke        Verify the isolated full-stack customer path"
	@echo "make up-fixture   Start the fixture-only development stack"
	@echo "make stop-safe    Drain the retained demo and stop it safely"

format:
	UV_CACHE_DIR=.uv-cache uv run --project tools/python-quality --frozen ruff format .
	UV_CACHE_DIR=.uv-cache uv run --project tools/python-quality --frozen ruff check --fix .

check:
	sh scripts/check.sh

smoke:
	python3 -m scripts.ci.isolated_smoke

up-fixture:
	docker compose -f docker-compose.yml up --build

stop-safe:
	python3 infra/lifecycle.py stop
