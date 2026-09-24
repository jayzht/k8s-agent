#!/usr/bin/env bash
# 一键搭建沙箱：kind 集群（1 控制面 + 4 worker）+ 25 个演示工作负载 + metrics-server。
#
# 关键点：本环境**无法访问 Docker Hub**，因此所有镜像都走可达的华为云镜像站。
# 这是环境约束，不是设计选择——生产环境应使用内网镜像仓库。
#
#   bash scripts/setup-sandbox.sh            # 建集群（已存在则跳过）
#   bash scripts/setup-sandbox.sh --recreate # 删掉重建（改节点数时用这个）
#
# ⚠️ 前置条件：宿主机的 fs.inotify.max_user_instances 要够大（见 kind-config.yaml 的说明）。
#    不满足时最隐蔽的症状是 kube-proxy 在部分节点 CrashLoopBackOff——
#    节点照样 Ready、演示应用照样跑，但 Service 路由是坏的。
#    本脚本结尾会显式检查这一点。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$ROOT/bin:$PATH"
# 本沙箱下 $HOME 只读，kind 无法写入默认的 ~/.kube/config，
# 因此把 kubeconfig 放在工作区内。
export KUBECONFIG="$ROOT/var/kubeconfig"
mkdir -p "$ROOT/var"

# 本沙箱下 $HOME/.docker 不可写，buildx 会因无法写 activity 文件而失败。
# 把 docker 配置目录重定向到工作区内。
export DOCKER_CONFIG="$ROOT/var/docker-config"
mkdir -p "$DOCKER_CONFIG"

MIRROR="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io"
NODE_IMAGE="$MIRROR/kindest/node:v1.37.0"
APP_IMAGE="om-demo-app:1"
CLUSTER=om-sandbox
RECREATE=0
[ "${1:-}" = "--recreate" ] && RECREATE=1

echo "== [1/9] 拉取 kind 节点镜像（经镜像站）=="
if ! docker image inspect "$NODE_IMAGE" >/dev/null 2>&1; then
  docker pull "$NODE_IMAGE"
else
  echo "已存在: $NODE_IMAGE"
fi

echo "== [2/9] 构建演示应用镜像 =="
docker build -q -t "$APP_IMAGE" "$ROOT/sandbox/app"
docker images "$APP_IMAGE" --format '{{.Repository}}:{{.Tag}}  {{.Size}}'

echo "== [3/9] 创建 kind 集群（1 控制面 + 4 worker）=="
if [ "$RECREATE" = "1" ] && kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "删除旧集群（节点数变了必须重建）..."
  kind delete cluster --name "$CLUSTER"
fi
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "集群已存在: $CLUSTER"
else
  kind create cluster --config "$ROOT/sandbox/kind-config.yaml" --image "$NODE_IMAGE" --wait 300s
fi

echo "== [4/9] 将应用镜像载入所有节点 =="
# 5 个节点都要有；漏一个就会出现「某个节点上的 Pod 起不来」的假故障。
kind load docker-image "$APP_IMAGE" --name "$CLUSTER"

echo "== [5/9] 生成清单 =="
python3 "$ROOT/sandbox/topology.py"

echo "== [6/9] 装 metrics-server（HPA 需要它）=="
# 没有 metrics-server 时，**所有 HPA 都会常年 ScalingActive=False /
# FailedGetResourceMetric**。那是一个一直存在、看起来很像"答案"的假信号：
# 问"HPA 坏了吗"，很容易照着它编出一个错误结论（真实发生过）。
MS_MANIFEST="$ROOT/sandbox/addons/metrics-server.yaml"
if [ -f "$MS_MANIFEST" ]; then
  MS_IMG=$(grep -oP '(?<=image: ).*' "$MS_MANIFEST" | head -1)
  docker pull -q "$MS_IMG" >/dev/null 2>&1 || true
  kind load docker-image "$MS_IMG" --name "$CLUSTER" >/dev/null 2>&1 || true
  kubectl apply -f "$MS_MANIFEST" >/dev/null
  kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s >/dev/null 2>&1 || true
  echo "  已部署: $MS_IMG"
else
  echo "  跳过：找不到 $MS_MANIFEST"
fi

echo "== [7/9] 部署演示负载 =="
kubectl apply -f "$ROOT/sandbox/manifests/00-namespaces.yaml"
kubectl apply -f "$ROOT/sandbox/manifests/10-demo.yaml"
kubectl apply -f "$ROOT/sandbox/manifests/20-staging.yaml"
kubectl apply -f "$ROOT/sandbox/manifests/30-observability.yaml"

echo "  等待 rollout 收敛（25 个工作负载）..."
for ns in demo staging observability; do
  kubectl -n "$ns" wait --for=condition=Available deployment --all --timeout=300s >/dev/null 2>&1 || true
done
# DaemonSet 没有 Available condition，单独等
kubectl -n observability rollout status daemonset/log-collector --timeout=180s >/dev/null 2>&1 || true
# StatefulSet 逐个等，避免 wait --all 在 PVC 还没绑定时报错
for sts in postgres-primary redis-cache session-store kafka-broker; do
  kubectl -n demo rollout status statefulset/$sts --timeout=180s >/dev/null 2>&1 || true
done

echo "== [8/9] 检查集群底座 =="
# ⚠️ kube-proxy 必须每个节点都 Running。它在部分节点崩掉时**节点依然是 Ready**，
# 演示应用也照跑（它不访问别的服务），但 Service 的 ClusterIP 路由是坏的——
# pod 连不上 apiserver，metrics-server 这类组件全废。
# 这是本沙箱最隐蔽的一种坏法，所以每次都显式查一遍。
kube_proxy_bad=$(kubectl -n kube-system get pods -l k8s-app=kube-proxy --no-headers 2>/dev/null \
  | awk '$2 != "1/1"' | wc -l | tr -d ' ')
if [ "$kube_proxy_bad" != "0" ]; then
  echo "  ⚠️  有 $kube_proxy_bad 个 kube-proxy 不正常 —— Service 路由可能是坏的。"
  echo "     多半是 fs.inotify.max_user_instances 不够。先执行："
  echo "       sysctl -w fs.inotify.max_user_instances=2048"
  echo "       sysctl -w fs.inotify.max_user_watches=524288"
  echo "     然后 kubectl -n kube-system rollout restart daemonset/kube-proxy"
else
  echo "  kube-proxy: 全部 Running ✓"
fi
ms_ready=$(kubectl -n kube-system get deploy metrics-server --no-headers 2>/dev/null | awk '{print $2}')
if [ "${ms_ready:-}" = "1/1" ]; then
  echo "  metrics-server: 就绪 ✓"
else
  echo "  ⚠️  metrics-server: ${ms_ready:-未安装}（HPA 会一直扩缩容不了）"
fi

echo "== [9/9] 验证 =="
kubectl get nodes -L topology.kubernetes.io/zone -L node-pool
echo
for ns in demo staging observability; do
  printf '%-16s ' "$ns"
  kubectl -n "$ns" get deploy,sts,ds --no-headers 2>/dev/null | wc -l | tr -d ' ' | xargs -I{} echo "{} 个工作负载"
done
echo
kubectl -n demo get pods --no-headers 2>/dev/null \
  | awk '{print $3}' | sort | uniq -c | sort -rn
echo "SANDBOX_OK"
