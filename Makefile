.PHONY: help install dev test lint format run dashboard engine docker docker-up docker-down backup clean

PYTHON := .venv/bin/python
PIP := .venv/bin/pip

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Setup ──────────────────────────────────────────────────────────

install: ## Install production dependencies
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e .

dev: ## Install with dev + prod dependencies
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev,prod]"

# ─── Quality ────────────────────────────────────────────────────────

test: ## Run all tests
	$(PYTHON) -m pytest tests/ -q

test-cov: ## Run tests with coverage report
	$(PYTHON) -m pytest tests/ --cov=src --cov-report=term-missing

lint: ## Run linter (ruff)
	$(PYTHON) -m ruff check src/ tests/

format: ## Auto-format code (ruff)
	$(PYTHON) -m ruff format src/ tests/
	$(PYTHON) -m ruff check --fix src/ tests/

typecheck: ## Run type checker (mypy)
	$(PYTHON) -m mypy src/

# ─── Run ────────────────────────────────────────────────────────────

run: dashboard ## Alias for dashboard

dashboard: ## Start dashboard (Flask dev server, port 2345)
	$(PYTHON) -m src.cli dashboard

engine: ## Start trading engine (headless)
	$(PYTHON) -m src.cli engine start

scan: ## Run a single market scan
	$(PYTHON) -m src.cli scan --limit 20

# ─── Production ─────────────────────────────────────────────────────

gunicorn: ## Start with gunicorn (production)
	$(PYTHON) -m gunicorn \
		--bind 0.0.0.0:2345 \
		--workers 2 \
		--threads 4 \
		--timeout 120 \
		--access-logfile - \
		src.dashboard.app:app

# ─── Docker ─────────────────────────────────────────────────────────

docker: ## Build Docker image
	docker build -t polymarket-bot .

docker-up: ## Start with docker compose
	docker compose up -d

docker-down: ## Stop docker compose
	docker compose down

docker-logs: ## Follow docker compose logs
	docker compose logs -f bot

# ─── Data ───────────────────────────────────────────────────────────

backup: ## Backup the SQLite database
	$(PYTHON) -c "from src.storage.backup import backup_database; print(backup_database())"

# ─── Clean ──────────────────────────────────────────────────────────

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info/ .mypy_cache/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
