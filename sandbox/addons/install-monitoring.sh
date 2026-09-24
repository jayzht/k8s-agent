#!/usr/bin/env bash
# 一键部署 monitoring 命名空间下的真实可观测组件：
#   Prometheus + kube-state-metrics + Loki + promtail
#
#   bash sandbox/addons/install-monitoring.sh
#
# 幂等：重复执行只是重新 apply，已存在的资源不会被删。
#
# 关于镜像（重要，这是环境约束不是设计选择）：
#   本环境**访问不了 Docker Hub 和 registry.k8s.io**，所以脚本会
#     1. 从清单里读出规范镜像名（prom/prometheus 等），
#     2. 经可达的华为云镜像站拉取，
#     3. 重新 tag 回规范名，
#     4. `kind load docker-image` 灌进 5 个节点。
#   清单里因此写的是规范名 + imagePullPolicy: IfNotPresent —— 节点上已有就不会联网拉。
#   这和 sandbox/addons/metrics-server.yaml 是同一个套路。
#
# 代理：只有拉镜像可能用得上 host 上的代理。kubectl 一律在 unset 代理下跑，
#       否则 kubectl 连不上本地 kind apiserver。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ADDONS="$ROOT/sandbox/addons"
export PATH="$ROOT/bin:$PATH"
export KUBECONFIG="$ROOT/var/kubeconfig"

CLUSTER="${KIND_CLUSTER:-om-sandbox}"
NS=monitoring
# 本环境唯一可达的镜像源（和 kind 节点镜像、metrics-server 同一个源）
MIRROR="swr.cn-north-4.myhuaweicloud.com/ddn-k8s"
# 联网代理：仅用于 docker pull，且仅直连失败时兜底。
# **故意不给默认值**——以前这里硬编码了一个内网代理地址，
# 既对别人没用，又把内网拓扑写进了公开仓库。需要就走 SANDBOX_PROXY 传。
PROXY="${SANDBOX_PROXY:-}"

MANIFESTS=(
  "$ADDONS/00-monitoring-namespace.yaml"
  "$ADDONS/prometheus.yaml"
  "$ADDONS/kube-state-metrics.yaml"
  "$ADDONS/loki.yaml"
  "$ADDONS/promtail.yaml"
)

echo "== [1/5] 检查集群与前置条件 =="
# kubectl 必须直连本地集群：代理开着会让所有请求走代理然后超时
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "找不到 kind 集群 '$CLUSTER'。先跑 scripts/setup-sandbox.sh" >&2
  exit 1
fi
if [ ! -f "$KUBECONFIG" ]; then
  echo "找不到 kubeconfig: $KUBECONFIG" >&2
  exit 1
fi
for f in "${MANIFESTS[@]}"; do
  [ -f "$f" ] || { echo "缺清单文件: $f" >&2; exit 1; }
done
nodes_ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready"' | wc -l | tr -d ' ')
echo "  kind 集群 '$CLUSTER' 存在，Ready 节点 ${nodes_ready} 个"

echo "== [2/5] 准备镜像（镜像站 -> 规范名 -> kind load）=="
# 从清单里抓 image:，保证脚本和 YAML 不会各说各话
mapfile -t IMAGES < <(grep -h -oP '^\s*image:\s*\K\S+' "${MANIFESTS[@]}" | sort -u)
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
  # 规范名 -> 镜像站路径：registry.k8s.io 原样带前缀，其余都是 Docker Hub
  case "$img" in
    registry.k8s.io/*) mirror_ref="$MIRROR/$img" ;;
    *)                  mirror_ref="$MIRROR/docker.io/$img" ;;
  esac
  echo "  拉取 $mirror_ref"
  if ! docker pull "$mirror_ref"; then
    echo "  直连失败，改用代理 $PROXY 重试"
    if ! https_proxy="$PROXY" http_proxy="$PROXY" docker pull "$mirror_ref"; then
      echo "  拉取失败: $mirror_ref" >&2
      exit 1
    fi
  fi
  docker tag "$mirror_ref" "$img"
done

echo "  灌入集群节点（5 个节点都要有，漏一个就有 Pod 起不来）..."
kind load docker-image "${IMAGES[@]}" --name "$CLUSTER"

echo "== [3/5] 应用清单 =="
for f in "${MANIFESTS[@]}"; do
  echo "  apply $(basename "$f")"
  kubectl apply -f "$f"
done

echo "== [4/5] 等待就绪 =="
kubectl -n "$NS" rollout status deployment/kube-state-metrics --timeout=180s
kubectl -n "$NS" rollout status deployment/loki --timeout=180s
kubectl -n "$NS" rollout status deployment/prometheus --timeout=180s
kubectl -n "$NS" rollout status daemonset/promtail --timeout=180s

echo "== [5/5] 结果 =="
kubectl -n "$NS" get deploy,daemonset,svc,pod -o wide
echo
echo "  服务入口（集群内 DNS）:"
echo "    Prometheus        http://prometheus.monitoring.svc.cluster.local:9090"
echo "    Loki              http://loki.monitoring.svc.cluster.local:3100"
echo "    kube-state-metrics http://kube-state-metrics.monitoring.svc.cluster.local:8080/metrics"
echo
echo "  本机验证（记得先 unset http_proxy https_proxy）:"
echo "    kubectl -n $NS port-forward svc/prometheus 9090:9090 &"
echo "    curl -s 'http://127.0.0.1:9090/api/v1/query?query=kube_pod_container_status_restarts_total'"
echo "    kubectl -n $NS port-forward svc/loki 3100:3100 &"
echo "    curl -s -G 'http://127.0.0.1:3100/loki/api/v1/query_range' --data-urlencode 'query={namespace=\"demo\"}' --data-urlencode 'limit=5'"
echo "MONITORING_OK"
