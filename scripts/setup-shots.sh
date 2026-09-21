#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/O&M-agent"; cd "$ROOT"
export https_proxy=http://172.26.192.221:7890 http_proxy=http://172.26.192.221:7890
# 沙箱下 $HOME 只读，浏览器必须装到工作区内
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/var/ms-playwright"
export HOME="$ROOT/var/ffhome"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" "$HOME"
[ -d .venv-shots ] || python3 -m venv .venv-shots
source .venv-shots/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet playwright
python -m playwright install chromium 2>&1 | tail -4
echo "SHOTS_READY"
