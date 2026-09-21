#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/O&M-agent"
cd "$ROOT"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
echo "== installing deps =="
python -m pip install --quiet \
  "kubernetes>=29.0.0" \
  "pyyaml>=6.0" \
  "rich>=13.0" \
  "pytest>=8.0" \
  "jsonschema>=4.0"
echo "== versions =="
python -c "import kubernetes, yaml, rich, pytest, jsonschema; print('kubernetes', kubernetes.__version__); print('pyyaml', yaml.__version__); print('rich', rich.__version__)"
echo "PYTHON_OK"
