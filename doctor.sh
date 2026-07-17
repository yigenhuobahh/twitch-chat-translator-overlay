#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif [[ -x .venv/Scripts/python.exe ]]; then
  PY=.venv/Scripts/python.exe
elif command -v python3 >/dev/null 2>&1 && python3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  PY=python3
elif command -v python >/dev/null 2>&1 && python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  PY=python
else
  echo "[FAIL] Need Python 3.10+ (python3 or python). Run bash install.sh first."
  exit 1
fi
if ! "$PY" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  echo "[FAIL] Selected Python must be runnable and version 3.10+: $PY"
  echo "       If this is an existing .venv, remove it and run bash install.sh."
  exit 1
fi


exec "$PY" scripts/render_cn_chat.py --doctor "$@"
