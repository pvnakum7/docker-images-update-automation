#!/usr/bin/env bash
# Run the tool locally (Linux/macOS/WSL/Git-Bash). Usage: scripts/run_local.sh [extra args for src.main]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate 2>/dev/null || . .venv/Scripts/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export DIU_EMAIL_ENABLED="${DIU_EMAIL_ENABLED:-false}"
python -m src.main --config config/settings.yaml "$@"
