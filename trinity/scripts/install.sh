#!/usr/bin/env bash
# Linux/macOS/WSL installer. Windows users: scripts\windows\install.ps1
set -euo pipefail
cd "$(dirname "$0")/.."            # trinity/
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
"$PY" -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "created .env (edit it)"; }
cd ..
python trinity/main.py --init-db
python trinity/main.py --check
(cd trinity && python -m pytest -q)
echo "Installed. Start paper trading: python trinity/scripts/run_paper.py"
