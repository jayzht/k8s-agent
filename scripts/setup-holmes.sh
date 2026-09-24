#!/usr/bin/env bash
# HolmesGPT 基座 Spike：在独立 venv 中安装，避免污染主环境。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# 需要代理请自行 export，脚本不写死内网地址

if [ ! -d .venv-holmes ]; then python3 -m venv .venv-holmes; fi
source .venv-holmes/bin/activate
python -m pip install --quiet --upgrade pip
echo "== 安装 holmesgpt =="
python -m pip install --quiet holmesgpt
echo "== 版本 =="
python -c "import importlib.metadata as m; print('holmesgpt', m.version('holmesgpt'))" 2>&1
echo "== CLI 入口 =="
which holmes || echo "(holmes 不在 PATH)"
holmes --help 2>&1 | head -25
echo "HOLMES_INSTALL_OK"
