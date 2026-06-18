#!/usr/bin/env bash
# THE one gate — the single door to the trunk. Static lints (globbed, so the set
# never drifts) + the structural-absence ledger + the unit suite. CI, the
# ops/hooks/pre-push hook, and `make gate` all call THIS one script, so "what the
# gate runs" has exactly one home and cannot diverge.
#
# Green here is the precondition for landing on main. It is the STATIC half of
# "done"; the RUNTIME half is the live-outcome meter (scripts/live_meter.py),
# which certifies a real request genuinely completes on the trunk build.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0

echo "== lints =="
shopt -s nullglob
lints=(scripts/lint_*.sh)
if [ ${#lints[@]} -eq 0 ]; then
    echo "  (no scripts/lint_*.sh yet — structural checks below carry the load)"
fi
for lint in "${lints[@]}"; do
    echo "-- $lint"
    bash "$lint" || fail=1
done
shopt -u nullglob

echo "== structural absences =="
.venv/bin/python tools/structural_absence_check.py || fail=1

echo "== unit suite =="
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/deep_integration.py || fail=1

echo ""
if [ "$fail" -ne 0 ]; then
    echo "GATE: RED — do not land. Fix the above."
    exit 1
fi
echo "GATE: GREEN"
