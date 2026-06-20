#!/usr/bin/env bash
# Operation B — the engineering doctrine has ONE home (prompts/_principles.md)
# and every reasoning/build persona INCLUDES it by reference, never by copy.
# The mechanical half: a persona that drops the include reds the gate, so the
# rigor can never silently fall out of a plan/build/verify call.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
PRIN="prompts/_principles.md"

if [ ! -s "$PRIN" ]; then
    echo "  MISSING or empty: $PRIN (the single source of engineering doctrine)"
    fail=1
fi

# Every reasoning/build persona must REFERENCE the doctrine, not paste it.
REQUIRED=(planning architecture refactor bug-analysis implementation do_with_claude_system)
for p in "${REQUIRED[@]}"; do
    f="prompts/$p.md"
    if [ ! -f "$f" ]; then
        echo "  MISSING persona: $f"
        fail=1
        continue
    fi
    if ! grep -q '{{include:_principles}}' "$f"; then
        echo "  $f does not {{include:_principles}} — reference the doctrine, never paste it"
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "lint_principles_include: RED — doctrine not single-homed + included everywhere"
    exit 1
fi
echo "lint_principles_include: GREEN — doctrine single-homed; all ${#REQUIRED[@]} reasoning/build personas include it"
