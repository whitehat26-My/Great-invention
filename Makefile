.PHONY: help install up down purge migrate revision seed reset test lint fmt fmt-check typecheck api worker beat simulate check

PY := .venv/bin/python
PIP := .venv/bin/pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.venv:
	python3 -m venv .venv && $(PIP) install -q --upgrade pip

install: .venv ## Install the project and dev dependencies
	$(PIP) install -q -e ".[dev]"

up: ## Start Postgres + Redis locally (no Docker needed)
	@bash scripts/dev_up.sh

down: ## Stop local Postgres + Redis
	@bash scripts/dev_down.sh

purge: ## Stop services and delete all local data
	@bash scripts/dev_down.sh --purge

migrate: ## Apply database migrations
	.venv/bin/alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	.venv/bin/alembic revision --autogenerate -m "$(m)"

seed: ## Load the demo restaurant (menu, recipes, suppliers, staff, tables)
	.venv/bin/restaurant-ai seed

reset: ## Drop the schema, re-migrate and re-seed
	.venv/bin/restaurant-ai reset-db --yes && $(MAKE) migrate && $(MAKE) seed

test: ## Run the full test suite
	.venv/bin/pytest

lint: ## Lint
	.venv/bin/ruff check src tests

fmt: ## Format and auto-fix
	.venv/bin/ruff format src tests && .venv/bin/ruff check --fix src tests

fmt-check: ## Verify formatting without changing anything (what CI runs)
	.venv/bin/ruff format --check src tests

typecheck: ## Static type check
	.venv/bin/mypy

check: lint fmt-check typecheck test ## Everything CI runs

api: ## Run the FastAPI webhook receiver
	.venv/bin/uvicorn restaurant_ai.api.main:app --reload --port 8000

worker: ## Run the Celery worker
	.venv/bin/celery -A restaurant_ai.worker.celery_app:celery_app worker -l info

beat: ## Run the Celery beat scheduler
	.venv/bin/celery -A restaurant_ai.worker.celery_app:celery_app beat -l info

simulate: ## Replay a full simulated service day end to end
	.venv/bin/restaurant-ai simulate-day --auto-approve
