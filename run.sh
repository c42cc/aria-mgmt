#!/usr/bin/env bash
# Start Aria. Kills any prior instance, reinstalls, launches fresh.
set -euo pipefail
cd "$(dirname "$0")"
exec make run
