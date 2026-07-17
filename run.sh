#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Prefer venv, then a working python3, then python (Windows Git Bash / Store stub).
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


if [[ ! -f scripts/render_cn_chat.py ]]; then
  echo "[FAIL] 请在仓库根目录运行"
  exit 1
fi

if [[ $# -eq 0 || "${1:-}" == "menu" ]]; then
  exec "$PY" scripts/job_wizard.py menu
fi

case "$1" in
  -h|--help|help)
    echo "用法:"
    echo "  bash run.sh              中文交互菜单"
    echo "  bash run.sh new          引导新建配置"
    echo "  bash run.sh list         列出 jobs/"
    echo "  bash run.sh <名称>       一键复用配置"
    echo "  bash run.sh <名称> <参数> 一键复用 + 转发额外 CLI 参数"
    echo "  bash run.sh doctor       环境检查"
    exit 0
    ;;
  new|init-job)
    exec "$PY" scripts/render_cn_chat.py --init-job
    ;;
  list)
    exec "$PY" scripts/render_cn_chat.py --list-jobs
    ;;
  doctor)
    exec "$PY" scripts/render_cn_chat.py --doctor
    ;;
esac

JOBARG="$1"
shift
# Interactive path prompt when job does not pin video/html (same as menu [2]).
# Extra CLI args after job name are forwarded to the pipeline via job_wizard.
if [[ $# -eq 0 ]]; then
  exec "$PY" scripts/job_wizard.py run "$JOBARG"
fi
# Strip optional -- separator
if [[ "${1:-}" == "--" ]]; then
  shift
fi
if [[ $# -eq 0 ]]; then
  exec "$PY" scripts/job_wizard.py run "$JOBARG"
fi
exec "$PY" scripts/job_wizard.py run "$JOBARG" "$@"
