# UCS top-level Makefile. The plan's "stale code" probe relies on this:
# `make run` always kills the prior bot, reinstalls the package in editable
# mode, then re-launches. There is no way to launch with stale code.

PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help run preflight bootstrap kill restart deps test anchor-test e2e-golden clean fmt audit-idempotency audit-dedup gate meter local-chat spark-serve spark-serve-verify spark-serve-bench spark-serve-stop meter-local

help:
	@echo "Targets:"
	@echo "  run          - kill prior bot, reinstall (editable), launch fresh"
	@echo "  gate         - the one door to main: lints + structural absences + unit suite"
	@echo "  meter        - the live-outcome meter: a real request on the trunk build (real API \$$)"
	@echo "  eval-calibrate - calibrate the judge over the labeled corpus (real API \$$); gates Task done"
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

# --- Local Spark Agent -----------------------------------------------------
# A local-brained chat window: an open-source model on the DGX Spark behind the
# Anthropic Messages API, driving the SAME agent loop + MCP fleet. SPARK_NODE
# overrides the node (default spark1).

# Serve the default model on the Spark and wait until /v1/models is healthy.
spark-serve:
	@$(PYTHON) scripts/spark_serve.py --node $${SPARK_NODE:-spark1} --start

# Verify the serving good states (capture + Gemini): /v1/models, chat,
# the tool_use round-trip (the #1 risk), cache_control tolerance, GPU residency.
spark-serve-verify:
	@$(PYTHON) scripts/spark_serve.py --node $${SPARK_NODE:-spark1}

# Bench both candidates behind one served name; recommend a default on tool-call
# reliability + tok/s. Heavy (each model downloads/loads).
spark-serve-bench:
	@$(PYTHON) scripts/spark_serve.py --node $${SPARK_NODE:-spark1} --bench

# Tear the server down (weights cache kept).
spark-serve-stop:
	@$(PYTHON) scripts/spark_serve.py --node $${SPARK_NODE:-spark1} --stop

# The runtime half of done for the LOCAL brain: a real do_with_claude request on
# the Spark model fires a real tool and answers; writes a <hash>.local.json receipt.
meter-local:
	@URL="$${ANTHROPIC_BASE_URL:-$$($(PYTHON) -c 'from src import spark; print(spark.serve_endpoint("spark1"))')}" ; \
	 $(PYTHON) scripts/live_meter.py --base-url "$$URL" --model local-brain

# Open the local chat window. Resolves the Spark endpoint, points the brain at
# it, and serves the browser UI. Refuses to start if the brain is unreachable
# (halt-don't-heal; no cloud fallback). Set LOCAL_CHAT_HOST=0.0.0.0 +
# LOCAL_CHAT_SECRET=... to reach it from your phone over Tailscale.
local-chat:
	@ANTHROPIC_BASE_URL="$${ANTHROPIC_BASE_URL:-$$($(PYTHON) -c 'from src import spark; print(spark.serve_endpoint("spark1"))')}" \
	 CLAUDE_MODEL="$${CLAUDE_MODEL:-local-brain}" \
	 $(PYTHON) -m src.local_chat_web
