.PHONY: setup test lint typecheck up down sanctions
setup:
	uv sync
	@[ -f .env ] || cp .env.example .env
test:
	uv run pytest
lint:
	uv run ruff check .
typecheck:
	uv run mypy app tests scripts
up:
	docker compose up -d --build
down:
	docker compose down -v
sanctions:
	uv run python scripts/load_sanctions.py
