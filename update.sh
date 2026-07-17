#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "======== 一键更新 / Update ========"
if [[ -e .git ]]; then
  echo "[1/3] git pull --ff-only"
  if ! git pull --ff-only; then
    echo "[FAIL] git pull failed. Update stopped."
    echo "       The remote history may have been rewritten."
    echo "       1. Back up only local .env, jobs, custom profiles,"
    echo "          and configs/launcher.local.yaml."
    echo "       2. Create a fresh clone in a new directory."
    echo "       3. Restore those local files into the fresh clone."
    exit 1
  fi
else
  echo "[FAIL] This directory is not a git checkout; source update is unavailable."
  echo "       ZIP/source-archive copies cannot update themselves."
  echo "       Download a fresh release or create a fresh clone instead."
  exit 1
fi

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif [[ -x .venv/Scripts/python.exe ]]; then
  PY=.venv/Scripts/python.exe
elif command -v python3 >/dev/null 2>&1 && python3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  PY=python3
elif command -v python >/dev/null 2>&1 && python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  PY=python
else
  echo "[FAIL] Python not found. Run bash install.sh first."
  exit 1
fi
if ! "$PY" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  echo "[FAIL] Selected Python must be runnable and version 3.10+: $PY"
  echo "       If this is an existing .venv, remove it and run bash install.sh."
  exit 1
fi


echo "[2/3] pip ($PY)"
if ! "$PY" -m pip install -U pip; then
  echo "[FAIL] pip upgrade failed. Update stopped."
  exit 1
fi
if [[ -f requirements.txt ]]; then
  if ! "$PY" -m pip install -r requirements.txt; then
    echo "[FAIL] dependency install failed. Update stopped."
    exit 1
  fi
else
  if ! "$PY" -m pip install -e .; then
    echo "[FAIL] project install failed. Update stopped."
    exit 1
  fi
fi

echo "[3/3] doctor"
"$PY" scripts/render_cn_chat.py --doctor
echo "更新完成。"
