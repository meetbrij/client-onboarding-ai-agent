.PHONY: setup test lint typecheck check up down sanctions
setup:
	uv sync
	@[ -f .env ] || cp .env.example .env
test:
	uv run pytest
lint:
	uv run ruff check .
	uv run ruff format --check .
# Exactly what the QA pipeline's 'Lint and types' job runs, plus the fixture run. Run it before every push.
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy app tests scripts
	uv run python -m onboarding.graph.build --all

typecheck:
	uv run mypy app tests scripts
up:
	docker compose up -d --build
down:
	docker compose down -v
sanctions:
	uv run python scripts/load_sanctions.py
