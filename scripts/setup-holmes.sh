#!/usr/bin/env bash
# HolmesGPT 基座 Spike：在独立 venv 中安装，避免污染主环境。
set -euo pipefail
ROOT="/home/ubuntu/O&M-agent"
cd "$ROOT"
export https_proxy=http://172.26.192.221:7890
export http_proxy=http://172.26.192.221:7890

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
