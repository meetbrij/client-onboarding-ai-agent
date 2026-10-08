.PHONY: setup test lint typecheck check evals up down sanctions
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
	uv run mypy app tests scripts evals
	uv run python -m onboarding.graph.build --all

typecheck:
	uv run mypy app tests scripts evals
up:
	docker compose up -d --build
down:
	docker compose down -v
sanctions:
	uv run python scripts/load_sanctions.py

# The deterministic eval gate (recorded KYC responses, fake LLM, no network). The live run needs keys: see docs/EVALS.md.
evals:
	uv run python -m evals.run --deterministic --out /tmp/eval-deterministic.json
