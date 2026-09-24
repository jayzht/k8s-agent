#!/usr/bin/env bash
set -euo pipefail
# 从脚本自身位置推断仓库根，别写死绝对路径——否则别人 clone 下来改不动
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
# 需要走代理的话自己 export HTTPS_PROXY / HTTP_PROXY，脚本不替你决定
# （这里以前硬编码了一个内网代理地址，公开仓库里不该出现这种东西）
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
