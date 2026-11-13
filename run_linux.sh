#!/usr/bin/env bash
# Cheape Deal on Linux / a VPS without Docker: sets everything up once, then starts it. Same engine as run_local.bat.
#   ./run_linux.sh              set up (first time) and start
#   ./run_linux.sh --setup-only set up only
#   PORT=8000 ./run_linux.sh    choose the port
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "Python 3.10+ is needed: sudo apt install python3 python3-venv"; exit 1; }
exec "$PY" scripts/local_run.py "$@"
