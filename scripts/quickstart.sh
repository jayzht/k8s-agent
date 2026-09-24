#!/usr/bin/env bash
#
# 一条命令从零到能用： docker compose 起一个能连上集群的 O&M Agent。
#
#   bash scripts/quickstart.sh
#
# 它会：
#   1. 检查 docker / compose / DEEPSEEK_API_KEY
#   2. 没有沙箱集群就建一个（kind，1 控制面 + 4 worker + 25 个工作负载）
#   3. 装上可观测性组件（Prometheus / Loki / kube-state-metrics / promtail）
#   4. 用 docker compose 起服务（host 网络，见 docker-compose.sandbox.yml 的说明）
#   5. 等到 /api/health 通了，把网址和**首次登录密码**打出来
#
# 只想要其中一部分：
#   SKIP_CLUSTER=1   集群已经有了，别动它
#   SKIP_MONITORING=1 不装 Prometheus / Loki
#
# 已经有集群、只想起服务的话，其实直接
#   docker compose -f docker-compose.sandbox.yml up
# 就够了，这个脚本主要价值在于**第一次**那遍。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="$ROOT/bin:$PATH"
export KUBECONFIG="$ROOT/var/kubeconfig"
COMPOSE=(docker compose -f docker-compose.sandbox.yml)
PORT="${PORT:-8765}"
# 只统计**本次运行**产生的日志。docker compose logs 会把之前那些容器的日志
# 一起吐出来，于是"首次启动只显示一次的密码"会在每次重跑时被重新打印一遍——
# 既误导人，又把一个密钥反复暴露在终端上。
RUN_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. 前置检查 -----------------------------------------------------------
step "1/5 检查环境"
command -v docker >/dev/null || die "没装 docker"
docker info >/dev/null 2>&1 || die "docker 守护进程没在跑（或当前用户没权限）"
docker compose version >/dev/null 2>&1 || die "缺 docker compose 插件"

# 模型 key：优先环境变量，其次仓库里的 .env（compose 也会读它）
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
[ -n "${DEEPSEEK_API_KEY:-}" ] || die "没配模型 key。写进 .env：DEEPSEEK_API_KEY=sk-..."
echo "  docker $(docker version --format '{{.Server.Version}}') ｜ compose OK ｜ 模型 key 已配置"

# --- 2. 沙箱集群 -----------------------------------------------------------
step "2/5 沙箱集群"
if [ "${SKIP_CLUSTER:-0}" = "1" ]; then
  echo "  SKIP_CLUSTER=1，跳过"
elif kubectl get nodes >/dev/null 2>&1; then
  echo "  已有可用集群：$(kubectl get nodes --no-headers | wc -l) 个节点，跳过创建"
else
  # inotify 限额不够的话 kind 的 worker 起不来（kubelet 报 too many open files）。
  # 这一步要 root，改不了就让用户自己来，不要假装成功。
  if [ "$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)" -lt 512 ]; then
    warn "fs.inotify.max_user_instances 偏小，kind 多节点可能起不来。建议先跑："
    warn "  sudo sysctl -w fs.inotify.max_user_instances=2048"
    warn "  sudo sysctl -w fs.inotify.max_user_watches=524288"
  fi
  echo "  没有集群，开始建（首次约 5~10 分钟）…"
  bash scripts/setup-sandbox.sh
fi

# 沙箱组件（metrics-server 等）也在这里面
if [ -f scripts/setup-sandbox.sh ] && [ "${SKIP_CLUSTER:-0}" != "1" ]; then
  if ! kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1; then
    warn "缺 metrics-server，get_metrics 会返回空（不影响其它功能）"
    warn "  补装：kubectl apply -f sandbox/addons/metrics-server.yaml"
  fi
fi

# --- 3. 可观测性组件 -------------------------------------------------------
step "3/5 可观测性组件（Prometheus / Loki）"
if [ "${SKIP_MONITORING:-0}" = "1" ]; then
  echo "  SKIP_MONITORING=1，跳过"
elif kubectl -n monitoring get deploy prometheus >/dev/null 2>&1; then
  echo "  monitoring 命名空间已就绪，跳过"
elif [ -f sandbox/addons/install-monitoring.sh ]; then
  echo "  安装中（首次约 2~3 分钟，要拉镜像）…"
  bash sandbox/addons/install-monitoring.sh || warn "安装失败——服务照常能起，只是少几个指标/日志工具"
else
  warn "没找到 sandbox/addons/install-monitoring.sh，跳过"
fi

# --- 4. 起服务 -------------------------------------------------------------
step "4/5 准备镜像并启动服务"
# 镜像来源按优先级：
#   1. OMAGENT_IMAGE 指定的仓库镜像（已发布的话一条 pull 就够，不用源码）
#   2. 本地已有 omagent:1.0.0
#   3. 从当前源码构建
if [ -n "${OMAGENT_IMAGE:-}" ]; then
  # 先看本地有没有。OMAGENT_IMAGE 既可能是仓库镜像（ghcr.io/x/y:1.0），
  # 也可能只是个本地名（omagent:1.0.0）——后者去 Docker Hub 拉必然失败，
  # 而且失败不该让整个流程挂掉。标签是不变的，本地有就用本地的。
  if docker image inspect "$OMAGENT_IMAGE" >/dev/null 2>&1; then
    echo "  本地已有 $OMAGENT_IMAGE"
  else
    echo "  从镜像仓库拉取 $OMAGENT_IMAGE"
    docker pull "$OMAGENT_IMAGE" \
      || die "拉取失败，本地也没有这个镜像：$OMAGENT_IMAGE
     （离线环境可以改为从源码构建：unset OMAGENT_IMAGE）"
  fi
elif docker image inspect omagent:1.0.0 >/dev/null 2>&1; then
  echo "  本地已有 omagent:1.0.0，跳过构建"
else
  echo "  从源码构建镜像…"
  if ! DOCKER_BUILDKIT=1 docker build -q -t omagent:1.0.0 . >/dev/null 2>&1; then
    warn "buildx 不可用，退回经典构建器"
    DOCKER_BUILDKIT=0 docker build -q -t omagent:1.0.0 . >/dev/null || die "镜像构建失败"
  fi
fi
KUBECONFIG_PATH="$ROOT/var/kubeconfig" "${COMPOSE[@]}" up -d --no-build
echo "  容器已启动"

# --- 5. 等就绪并给出登录信息 -----------------------------------------------
step "5/5 等待就绪"
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then break; fi
  [ "$i" = "40" ] && die "等了 80 秒还没起来。看日志：${COMPOSE[*]} logs"
  sleep 2
done

# 两份日志，用途不同：
#   LOGS_ALL —— 横幅（集群状态、数据源）。容器被复用时不会有新日志，
#               所以这里必须取全量，否则横幅是空的。
#   LOGS_NEW —— 只取本次运行产生的，用来判断"这次到底有没有自举出新密码"。
LOGS_ALL="$("${COMPOSE[@]}" logs 2>&1 || true)"
LOGS_NEW="$("${COMPOSE[@]}" logs --since "$RUN_STARTED" 2>&1 || true)"
PASSWORD="$(printf '%s' "$LOGS_NEW" | grep -oP '(?<=密码    ).*' | tail -1 || true)"
CLUSTER_LINE="$(printf '%s' "$LOGS_ALL" | grep -m1 '集群：' || true)"
DATASOURCES="$(printf '%s' "$LOGS_ALL" | grep '数据源：' || true)"

if [ -n "$PASSWORD" ]; then
  cat <<EOF

────────────────────────────────────────────────────────────
  好了。

  $(echo "$CLUSTER_LINE" | sed 's/^omagent-1 *| *//')
$(echo "$DATASOURCES" | sed 's/^omagent-1 *| */  /')

  打开： http://127.0.0.1:${PORT}

  ⚠️ 首次启动自动创建的账号（只显示这一次，请立刻改密码）：
       用户名  admin
       密码    ${PASSWORD}

  改密码： ${COMPOSE[*]} exec omagent omagent passwd admin
  看日志： ${COMPOSE[*]} logs -f
  停掉：   ${COMPOSE[*]} down   （数据在卷里，不会丢；加 -v 才清）
────────────────────────────────────────────────────────────
EOF
else
  # 没有密码**不是出错**：自举只在数据卷里还没有用户库时发生一次。
  # 这里以前会打一句"没抓到密码"，看着像坏了——其实是"你之前已经建过了"。
  cat <<EOF

────────────────────────────────────────────────────────────
  好了。

  $(echo "$CLUSTER_LINE" | sed 's/^omagent-1 *| *//')
$(echo "$DATASOURCES" | sed 's/^omagent-1 *| */  /')

  打开： http://127.0.0.1:${PORT}

  这次没有生成新密码——数据卷里已经有用户库了（自举只在第一次发生）。
  忘了密码就重置：

     ${COMPOSE[*]} exec omagent omagent passwd admin

  想从头来过（**会清掉会话、审计链、案例库**）：

     ${COMPOSE[*]} down -v

  看日志： ${COMPOSE[*]} logs -f
────────────────────────────────────────────────────────────
EOF
fi

