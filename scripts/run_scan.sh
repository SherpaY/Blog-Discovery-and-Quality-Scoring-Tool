#!/bin/bash
# Manual scan runner. Usage: scripts/run_scan.sh [extra args, e.g. --window 72]
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m scanner scan "$@"
