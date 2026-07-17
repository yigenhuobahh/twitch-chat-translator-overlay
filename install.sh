#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "======== 一键安装 / Install ========"
echo "仓库: $(pwd)"

pick_python() {
  # Prefer existing venv, then a working python3, then python (Windows Store stub).
  if [[ -x .venv/bin/python ]]; then
    echo .venv/bin/python
    return
  fi
  if [[ -x .venv/Scripts/python.exe ]]; then
    echo .venv/Scripts/python.exe
    return
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
    echo python3
    return
  fi
  if command -v python >/dev/null 2>&1 && python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
    echo python
    return
  fi
  echo ""
}

resolve_venv_python() {
  if [[ -x .venv/bin/python ]]; then
    echo .venv/bin/python
  elif [[ -x .venv/Scripts/python.exe ]]; then
    echo .venv/Scripts/python.exe
  else
    echo ""
  fi
}

PY="$(pick_python)"
if [[ -z "$PY" ]]; then
  echo "[FAIL] 需要 Python 3.10+"
  exit 1
fi
if ! "$PY" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  echo "[FAIL] Selected Python must be runnable and version 3.10+: $PY"
  echo "       If this is an existing .venv, remove it and run bash install.sh."
  exit 1
fi


echo "[1/5] Python: $PY"
if [[ -z "$(resolve_venv_python)" ]]; then
  echo "[2/5] 创建 .venv"
  "$PY" -m venv .venv
else
  echo "[2/5] .venv 已存在"
fi
PY="$(resolve_venv_python)"
if [[ -z "$PY" ]]; then
  echo "[FAIL] .venv created but python not found under bin/ or Scripts/"
  exit 1
fi
if ! "$PY" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
  echo "[FAIL] Virtual environment must be runnable with Python 3.10+: $PY"
  echo "       Remove .venv and run bash install.sh again."
  exit 1
fi


echo "[3/5] 安装依赖"
"$PY" -m pip install -U pip
if [[ -f requirements.txt ]]; then
  "$PY" -m pip install -r requirements.txt
else
  "$PY" -m pip install -e .
fi

echo "[4/5] --init + --doctor"
"$PY" scripts/render_cn_chat.py --init
set +e
"$PY" scripts/render_cn_chat.py --doctor
DOC_RC=$?
set -e
if [[ "$DOC_RC" -ne 0 ]]; then
  echo
  echo "[WARN] 环境未完全就绪（常见：FFmpeg / CJK 字体）"
  echo "       交互终端下 doctor 会询问是否帮你安装 FFmpeg（默认 Yes）。"
  echo "       复检: bash run.sh doctor"
  echo "       或: $PY scripts/render_cn_chat.py --doctor --offer-fix"
  exit "$DOC_RC"
fi

echo
echo "[5/5] 可选增强: TwitchDownloaderCLI（从链接下 VOD/聊天，免 GUI）"
if [[ -z "${CI:-}" ]]; then
  "$PY" scripts/render_cn_chat.py --install-td-prompt || true
else
  echo "  CI: 跳过可选 CLI 询问"
fi

echo
echo "======== 安装完成 ========"
echo "下一步: 编辑 .env（如需翻译）→ bash run.sh → 新建/下载素材/复用配置"
echo "  下载: $PY scripts/render_cn_chat.py --download <url>"
