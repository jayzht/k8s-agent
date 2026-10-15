#!/usr/bin/env bash
# 一键部署 Chaos Mesh（第二套故障注入器）。
#
#   bash sandbox/addons/install-chaos-mesh.sh
#
# 幂等：重复执行只是重新 apply，已存在的资源不会被删。
#
# 为什么需要这个脚本，而不是一条 `kubectl apply`
# ==============================================
# 官方装法是
#   curl -sSL https://mirrors.chaos-mesh.org/v2.6.3/install.yaml | kubectl apply -f -
# 但**这个环境里两件事都行不通**：
#
#   1. mirrors.chaos-mesh.org 访问不了（实测所有版本都返回 S3 的
#      `403 AccessDenied`），GitHub release 里也没有 install.yaml 资产。
#      → 所以清单是从官方 Helm chart 渲染后 **vendored 进仓库**的
#        （见 sandbox/addons/chaos-mesh/install.yaml 顶部的说明）。
#
#   2. **kind 节点拉不动外网镜像**。实测在节点里 crictl pull 会
#      `read: connection reset by peer`。所以镜像必须走
#      「宿主机 docker pull → kind load docker-image」，而且
#      `kind load docker-image` 要确认灌进了**全部 5 个节点**，
#      漏一个节点就会有 chaos-daemon Pod 起不来。
#
# 关于代理（重要，这是环境约束不是设计选择）
# ==========================================
# 宿主机 `docker pull` 在本环境里要**显式挂代理**才通。所以代理地址
# 走 SANDBOX_PROXY 传进来，**故意不给默认值**——以前别的脚本里硬编码过
# 一个内网代理地址，既对别人没用，又把内网拓扑写进了公开仓库。
#
#   SANDBOX_PROXY=http://<host>:<port> bash sandbox/addons/install-chaos-mesh.sh
#
# kubectl / kind 一律在 **unset 代理** 下跑，否则它们会试图通过代理连
# 本地 kind apiserver，然后全部超时。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$ROOT/sandbox/addons/chaos-mesh/install.yaml"
export PATH="$ROOT/bin:$PATH"
export KUBECONFIG="$ROOT/var/kubeconfig"

CLUSTER="${KIND_CLUSTER:-om-sandbox}"
NS=chaos-mesh
PROXY="${SANDBOX_PROXY:-}"
# 华为云镜像站：本环境实测可达的镜像源（monitoring 那套装的就是这条路）。
# Chaos Mesh 的镜像在 ghcr.io 上，这里只是**兜底尝试**，不一定有；
# 真正的正路是挂代理 docker pull。
MIRROR="swr.cn-north-4.myhuaweicloud.com/ddn-k8s"

echo "== [1/5] 检查集群与前置条件 =="
# kubectl/kind 必须直连本地集群：代理开着会让所有请求走代理然后超时
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "找不到 kind 集群 '$CLUSTER'。先跑 scripts/setup-sandbox.sh" >&2
  exit 1
fi
[ -f "$KUBECONFIG" ] || { echo "找不到 kubeconfig: $KUBECONFIG" >&2; exit 1; }
[ -f "$MANIFEST" ] || { echo "缺清单文件: $MANIFEST" >&2; exit 1; }
nodes_ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready"' | wc -l | tr -d ' ')
echo "  kind 集群 '$CLUSTER' 存在，Ready 节点 ${nodes_ready} 个"

echo "== [2/5] 准备镜像（docker pull -> kind load，5 个节点都要有）=="
# 从清单里抓 image:，保证脚本和 YAML 不会各说各话
mapfile -t IMAGES < <(grep -h -oP '^\s*image:\s*\K\S+' "$MANIFEST" | sort -u)
if [ "${#IMAGES[@]}" -eq 0 ]; then
  echo "  没有从清单里解析到 image，检查 YAML 缩进" >&2
  exit 1
fi
printf '  清单需要的镜像: %s\n' "${IMAGES[*]}"

for img in "${IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    echo "  已有本地镜像: $img"
    continue
  fi
  pulled=""
  # 顺序：直连 -> 代理 -> 镜像站。本环境实测**直连必失败、代理通**。
  if docker pull "$img" >/dev/null 2>&1; then
    pulled=direct
  elif [ -n "$PROXY" ] && http_proxy="$PROXY" https_proxy="$PROXY" docker pull "$img" >/dev/null 2>&1; then
    pulled=proxy
  elif docker pull "$MIRROR/$img" >/dev/null 2>&1; then
    docker tag "$MIRROR/$img" "$img"
    pulled=mirror
  fi
  if [ -z "$pulled" ]; then
    echo "  拉取失败: $img" >&2
    echo "  本环境的 kind 节点拉不动外网镜像，宿主机 docker pull 也要挂代理。" >&2
    echo "  请带上代理重试：SANDBOX_PROXY=http://<host>:<port> bash $0" >&2
    exit 1
  fi
  echo "  拉取成功（$pulled）: $img"
done

echo "  灌入集群节点（5 个节点都要有，漏一个就有 chaos-daemon 起不来）..."
kind load docker-image "${IMAGES[@]}" --name "$CLUSTER"

echo "== [3/5] 创建命名空间并 apply =="
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
# ⚠️ --server-side 是**必须的**：其中 3 个 CRD 的 schema 太大，
# 客户端 apply 会把 `last-applied-configuration` 注解撑过 262144 字节，
# API Server 直接拒收（"metadata.annotations: Too long"）。
kubectl apply --server-side --force-conflicts -f "$MANIFEST"

echo "== [4/5] 等待就绪 =="
kubectl -n "$NS" rollout status deployment/chaos-controller-manager --timeout=300s
kubectl -n "$NS" rollout status deployment/chaos-dns-server --timeout=300s
kubectl -n "$NS" rollout status daemonset/chaos-daemon --timeout=300s

echo "== [5/5] 结果 =="
kubectl -n "$NS" get pods -o wide
echo
echo "  关键 CRD:"
kubectl get crd 2>/dev/null | grep chaos-mesh.org \
  | awk '{print "    " $1}' | grep -E 'podchaos|networkchaos|stresschaos|dnschaos|iochaos' || true
echo
echo "  自检："
bash "$ROOT/sandbox/chaos.sh" status || true
