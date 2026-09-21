#!/usr/bin/env bash
# 一键搭建沙箱：kind 集群 + 演示应用。
#
# 关键点：本环境**无法访问 Docker Hub**，因此所有镜像都走可达的华为云镜像站。
# 这是环境约束，不是设计选择——生产环境应使用内网镜像仓库。
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

echo "== [1/6] 拉取 kind 节点镜像（经镜像站）=="
if ! docker image inspect "$NODE_IMAGE" >/dev/null 2>&1; then
  docker pull "$NODE_IMAGE"
else
  echo "已存在: $NODE_IMAGE"
fi

echo "== [2/6] 构建演示应用镜像 =="
docker build -q -t "$APP_IMAGE" "$ROOT/sandbox/app"
docker images "$APP_IMAGE" --format '{{.Repository}}:{{.Tag}}  {{.Size}}'

echo "== [3/6] 创建 kind 集群 =="
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "集群已存在: $CLUSTER"
else
  kind create cluster --name "$CLUSTER" --image "$NODE_IMAGE" --wait 300s
fi

echo "== [4/6] 将应用镜像载入集群 =="
kind load docker-image "$APP_IMAGE" --name "$CLUSTER"

echo "== [5/6] 部署演示负载 =="
kubectl apply -f "$ROOT/sandbox/app.yaml"
kubectl -n demo rollout status deployment/api-gateway --timeout=240s
kubectl -n demo rollout status deployment/billing-core --timeout=240s
kubectl -n demo rollout status statefulset/session-store --timeout=240s || true

echo "== [6/6] 验证 =="
kubectl get nodes
kubectl -n demo get pods -o wide
kubectl -n demo get pdb
echo "SANDBOX_OK"
