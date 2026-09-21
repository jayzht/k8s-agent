#!/usr/bin/env bash
set -euo pipefail
ROOT="/home/ubuntu/O&M-agent"
BIN="$ROOT/bin"
mkdir -p "$BIN"

echo "== [1/4] install kubectl =="
if [ ! -x "$BIN/kubectl" ]; then
  KVER=$(curl -sSL https://dl.k8s.io/release/stable.txt)
  echo "kubectl version: $KVER"
  curl -sSLo "$BIN/kubectl" "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl"
  chmod +x "$BIN/kubectl"
fi
"$BIN/kubectl" version --client

echo "== [2/4] install kind =="
if [ ! -x "$BIN/kind" ]; then
  # GitHub API is rate-limited here, so use the direct "latest" download URL.
  curl -sSLo "$BIN/kind" "https://github.com/kubernetes-sigs/kind/releases/latest/download/kind-linux-amd64"
  chmod +x "$BIN/kind"
fi
"$BIN/kind" version

echo "== [3/4] create kind cluster 'om-sandbox' =="
if ! "$BIN/kind" get clusters 2>/dev/null | grep -qx om-sandbox; then
  "$BIN/kind" create cluster --name om-sandbox --wait 180s
else
  echo "cluster already exists"
fi

echo "== [4/4] verify =="
export KUBECONFIG="$HOME/.kube/config"
"$BIN/kubectl" cluster-info
"$BIN/kubectl" get nodes -o wide
echo "SETUP_OK"
