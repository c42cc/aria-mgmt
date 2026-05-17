# UCS top-level Makefile. The plan's "stale code" probe relies on this:
# `make run` always kills the prior bot, reinstalls the package in editable
# mode, then re-launches. There is no way to launch with stale code.

PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help run preflight bootstrap kill restart deps test anchor-test clean fmt audit-idempotency audit-dedup

help:
	@echo "Targets:"
	@echo "  run          - kill prior bot, reinstall (editable), launch fresh"
	@echo "  preflight    - run capability preflight (same probes the bot runs at boot)"
	@echo "  bootstrap    - one-command fresh-machine setup (venv, npm, MCP, OAuth, prompts)"
	@echo "  kill         - kill any running bot processes"
	@echo "  restart      - alias for run"
	@echo "  deps         - regenerate requirements.txt from pip freeze (drift check)"
	@echo "  test         - run smoke + deep integration tests"
	@echo "  anchor-test  - run anchor pressure-test suite (20 tasks, ~5min)"
	@echo "  fmt          - format Python (ruff)"
	@echo "  audit-idempotency - AST-check that connect/start/open/join begin with idempotency guard"
	@echo "  audit-dedup  - scan data/audit.jsonl for duplicate API calls inside 5s windows"
	@echo "  clean        - remove __pycache__, .pytest_cache"

# Always reinstall in editable mode so the freshly running process matches
# the source on disk (kills the "stale launch" bug class).
run: kill
	@$(PIP) install -e . --quiet
	@echo "Launching bot..."
	@$(PYTHON) -m src.bot

restart: run

preflight:
	@$(PYTHON) -m src.preflight

bootstrap:
	@bash ops/bootstrap.sh

kill:
	@pkill -f "src.bot" 2>/dev/null || true
	@sleep 1

deps:
	@$(PIP) freeze > requirements.txt
	@echo "requirements.txt regenerated."

test:
	@$(PYTHON) tests/smoke.py
	@$(PYTHON) tests/deep_integration.py

anchor-test:
	@$(PYTHON) tests/anchor_suite/run.py

fmt:
	@$(PYTHON) -m ruff format src/ tests/ 2>/dev/null || echo "ruff not installed"

audit-idempotency:
	@$(PYTHON) -m src.audit_idempotency_lint

audit-dedup:
	@$(PYTHON) -m src.audit_dedup_probe

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
