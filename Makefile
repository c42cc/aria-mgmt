.PHONY: run test gate spark-serve spark-serve-verify spark-serve-bench spark-serve-stop

PYTHON ?= .venv/bin/python
SPARK_NODE ?= spark1

run:
	$(PYTHON) -m src.bot

test:
	$(PYTHON) -m pytest -q

# The static half of "done". The runtime half is a real request via `make run`
# completing and being honestly logged in data/outcomes.jsonl.
gate: test
	@echo "GATE GREEN (static). Runtime half = a real request that delivers + logs honestly."

# ── The Mind (Spark A) — serve local-brain behind the inference contract ──
# vLLM serves the Anthropic Messages API natively; the Mac side only points
# SPARK_BASE_URL at it. Idempotent + loud (scripts/spark_serve.py -> src/spark.py).
spark-serve:
	@$(PYTHON) scripts/spark_serve.py --node $(SPARK_NODE) --start

spark-serve-verify:
	@$(PYTHON) scripts/spark_serve.py --node $(SPARK_NODE)

spark-serve-bench:
	@$(PYTHON) scripts/spark_serve.py --node $(SPARK_NODE) --bench

spark-serve-stop:
	@$(PYTHON) scripts/spark_serve.py --node $(SPARK_NODE) --stop
