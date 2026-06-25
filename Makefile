.PHONY: run test gate doctor home spark-serve spark-serve-verify spark-serve-bench spark-serve-stop

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

# The single pane — every plane's live health (Mind/Hands/Floor/HA/Cloud + spend).
doctor:
	@$(PYTHON) -m src.doctor

# The front door — deploy + self-refresh the one-page status to the Mind, served
# (no auth) at http://100.106.152.104:8123/local/home.html (local net + Tailscale).
home:
	@$(PYTHON) scripts/publish_home.py

# Zed oversight: open the Hands (Spark) dev environment over the mesh (Tailscale
# SSH). The workspace surfaces live cell runs at .cells/ and the cell branches —
# watch and directly steer what the dev environment is doing, from anywhere.
zed-hands:
	zed "ssh://$(SPARK_CELL_NODE)/home/$(SPARK_CELL_NODE)/live_visuals_4"

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
