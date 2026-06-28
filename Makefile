# UCS top-level Makefile. The plan's "stale code" probe relies on this:
# `make run` always kills the prior bot, reinstalls the package in editable
# mode, then re-launches. There is no way to launch with stale code.

PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help run preflight bootstrap kill restart deps test anchor-test e2e-golden clean fmt audit-idempotency audit-dedup gate meter fulfillment-golden fulfillment-calibrate fulfillment-pressure fulfillment-reverify fulfillment-report notify-heartbeat install-notify-heartbeat uninstall-notify-heartbeat

help:
	@echo "Targets:"
	@echo "  run          - kill prior bot, reinstall (editable), launch fresh"
	@echo "  gate         - the one door to main: lints + structural absences + unit suite"
	@echo "  meter        - the live-outcome meter: a real request on the trunk build (real API \$$)"
	@echo "  eval-calibrate - calibrate the judge over the labeled corpus (real API \$$); gates Task done"
	@echo "  fulfillment-{golden,calibrate,pressure,reverify,report} - the request-fulfillment harness (real API \$$)"
	@echo "  preflight    - run capability preflight (same probes the bot runs at boot)"
	@echo "  bootstrap    - one-command fresh-machine setup (venv, npm, MCP, OAuth, prompts)"
	@echo "  kill         - kill any running bot processes"
	@echo "  restart      - alias for run"
	@echo "  deps         - regenerate requirements.txt from pip freeze (drift check)"
	@echo "  test         - run smoke + deep integration tests"
	@echo "  anchor-test  - run anchor pressure-test suite (20 tasks, ~5min)"
	@echo "  e2e-golden   - run unified Aria voice+text golden-path E2E (~10min, ~\$$1-2)"
	@echo "  fmt          - format Python (ruff)"
	@echo "  audit-idempotency - AST-check that connect/start/open/join begin with idempotency guard"
	@echo "  audit-dedup  - scan data/audit.jsonl for duplicate API calls inside 5s windows"
	@echo "  clean        - remove __pycache__, .pytest_cache"

# One launch path (ops/launch.sh): pin to the trunk (git checkout main), reinstall
# in editable mode so the running process matches source, then exec the bot. The
# separate `kill` target runs first (launch.sh never pkills — that would crash-loop
# launchd). Same script the launchd KeepAlive and deploy.sh restart use.
run: kill
	@bash ops/launch.sh

restart: run

gate:
	@bash scripts/gate.sh

meter:
	@$(PYTHON) scripts/live_meter.py

eval-calibrate:
	@$(PYTHON) -m src.judge_calibration

# Request-fulfillment harness (advisory; intent-first, arc-complete). golden is
# the R5 definition-of-done; pressure is the world-center adversarial suite;
# reverify shows the §7 dispatch fix close the dispatch-context dysfunction.
fulfillment-golden:
	@$(PYTHON) -m src.fulfillment golden

fulfillment-calibrate:
	@$(PYTHON) -m src.fulfillment calibrate

fulfillment-pressure:
	@$(PYTHON) -m src.fulfillment pressure

fulfillment-reverify:
	@$(PYTHON) -m src.fulfillment reverify

# A live chief-of-staff scorecard over the running bot's real records.
fulfillment-report:
	@$(PYTHON) -m src.fulfillment report --data-dir data --hours 72

# The notify path's self-test + its launchd schedule. `notify-heartbeat` runs
# the REAL delivery once (proof to your phone, loud alarm if the path is down);
# install/uninstall manage the daily launchd job (runs at load + 09:00).
notify-heartbeat:
	@$(PYTHON) src/notify_phone.py heartbeat

install-notify-heartbeat:
	@launchctl bootout gui/$$(id -u)/com.you.aria-notify-heartbeat 2>/dev/null || true
	@launchctl bootstrap gui/$$(id -u) ops/com.you.aria-notify-heartbeat.plist
	@echo "notify heartbeat installed (runs at load + daily 09:00)"

uninstall-notify-heartbeat:
	@launchctl bootout gui/$$(id -u)/com.you.aria-notify-heartbeat 2>/dev/null || true
	@echo "notify heartbeat removed"

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

# Primary E2E test going forward — exercises every verbal request type Aria
# handles through her real voice path against a real bot. Owns the bot
# lifecycle (kill -> reinstall -> start -> preflight wait -> run -> kill).
# Requires the operator to be in the #general voice channel so Aria can
# auto-connect. ~10 min wall time, ~$1-2 in real API calls.
e2e-golden:
	@$(PYTHON) scripts/e2e_aria_golden.py

fmt:
	@$(PYTHON) -m ruff format src/ tests/ 2>/dev/null || echo "ruff not installed"

audit-idempotency:
	@$(PYTHON) -m src.audit_idempotency_lint

audit-dedup:
	@$(PYTHON) -m src.audit_dedup_probe

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
