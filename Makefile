.PHONY: run test gate

run:
	.venv/bin/python -m src.bot

test:
	.venv/bin/python -m pytest -q

# The static half of "done". The runtime half is a real request via `make run`
# completing and being honestly logged in data/outcomes.jsonl.
gate: test
	@echo "GATE GREEN (static). Runtime half = a real request that delivers + logs honestly."
