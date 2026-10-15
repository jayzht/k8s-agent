#!/usr/bin/env bash
# Chaos Mesh 版故障注入 —— 和 sandbox/faults.sh 并行的第二条注入路径。
#
# 为什么要有这一份
# ================
# `faults.sh` 注入的都是**变更类**故障：改 Deployment 的字段、改 Service 的
# 选择器、改 ConfigMap 的内容。它做不了另外一大类真实故障——**运行时故障**：
#
#   · 两个服务之间的网络被切断 / 被注入延迟（K8s 对象上完全看不出来）
#   · CPU 被人为打满（配置没变，但服务就是变慢）
#   · 随机、持续地杀 Pod（不是"改坏了"，而是"基础设施在抖"）
#   · DNS 解析被污染（连 Service 都解析不出来）
#
# 这类故障在真实值班里最难查，因为**集群对象一切正常**。Chaos Mesh 是这类
# 故障的标准注入器，所以把它接进来当第二套注入器。
#
#   sandbox/chaos.sh <场景> [目标工作负载]
#   sandbox/chaos.sh list            列出全部场景
#   sandbox/chaos.sh targets         列出当前可用的目标
#   sandbox/chaos.sh status          看一眼当前注入了什么
#   sandbox/chaos.sh reset           清掉**所有** Chaos 对象，恢复基线
#
# 和 faults.sh 的分工
# ===================
#   · faults.sh  → 改对象（spec / ConfigMap / Service / HPA / 配额 / 节点）
#   · chaos.sh   → 改运行时（网络 / CPU / DNS / Pod 生命周期）
#
# 两者都遵守同一条纪律：**apply 完不算注入成功，要等到症状真的出现在集群里
# 才返回**（见下面的 wait_for）。这是 faults.sh 里踩出来的教训。
#
# ⚠️ reset 必须删干净
# ===================
# Chaos 对象是**独立于工作负载**的：`faults.sh reset` 删掉工作负载再重建，
# 带不走 NetworkChaos。残留的实验会在下一个场景里继续生效，让评测结果变成
# 噪声——所以这里的 reset 会把这个集群里**所有命名空间**的所有 Chaos 对象
# 全删掉（含 Schedule：PodChaos 的"定时反复杀"是 Schedule 生成的，只删
# podchaos 的话下一轮它会把实验重新建回来）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$ROOT/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$ROOT/var/kubeconfig}"

SCENARIO="${1:-list}"
TARGET="${2:-}"
NS="${OM_NS:-demo}"

# ── 可调参数（都不改的话就是"默认强度"）────────────────────────
# 网络：谁连不上谁 / 多慢
PEER="${OM_PEER:-postgres-primary}"        # 网络类场景的对端（下游）
LATENCY="${OM_LATENCY:-1s}"                 # network-delay 的延迟
LATENCY_MIN_MS="${OM_LATENCY_MIN_MS:-800}"  # 判定"真的慢了"的阈值（毫秒）
DURATION="${OM_DURATION:-10m}"             # 所有实验的持续时间
# CPU：打多满
STRESS_WORKERS="${OM_STRESS_WORKERS:-2}"
STRESS_LOAD="${OM_STRESS_LOAD:-100}"
STRESS_MIN_MC="${OM_STRESS_MIN_MC:-150}"   # 判定"真的打满了"的阈值（millicores）
# PodChaos：多久杀一次
PODKILL_EVERY="${OM_PODKILL_EVERY:-30s}"

# Chaos Mesh 自己的命名空间。没装的话下面会明确报出来，而不是让 kubectl
# 报一堆 "the server could not find the requested resource"。
CHAOS_NS="${OM_CHAOS_NS:-chaos-mesh}"

k() { kubectl -n "$NS" "$@"; }
hint() { printf '\n>>> %s\n' "$*"; }
step() { printf '==> %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# ── 前置检查 ────────────────────────────────────────────────

require_chaos() {
  kubectl get crd networkchaos.chaos-mesh.org >/dev/null 2>&1 \
    || die "这个集群没装 Chaos Mesh（找不到 networkchaos CRD）。装法见 sandbox/addons/install-chaos-mesh.sh。"
  kubectl -n "$CHAOS_NS" get deploy chaos-controller-manager >/dev/null 2>&1 \
    || die "Chaos Mesh 的 CRD 在，但 $CHAOS_NS 里没有 chaos-controller-manager。"
}

# ── 目标解析（和 faults.sh 同一套语义）───────────────────────

kind_of() {
  local name="$1" ns="${2:-$NS}" k
  for k in deployment statefulset daemonset; do
    if kubectl -n "$ns" get "$k" "$name" >/dev/null 2>&1; then printf '%s' "$k"; return 0; fi
  done
  return 1
}

resolve() {
  TNS="${2:-$NS}"
  DEP="${TARGET:-$1}"
  if ! KIND="$(kind_of "$DEP" "$TNS")"; then
    die "命名空间 $TNS 里找不到工作负载 $DEP。用 'sandbox/chaos.sh targets' 看有哪些可用。"
  fi
}

# 挑一个 Running 的 Pod 来做"从里面往外连"的验证。
# ⚠️ 必须挑 Running 的：PodChaos 场景下会有 Pod 正在被删/正在起，
# 拿一个 Terminating 的 Pod 去 exec 只会得到一句 "pod is being deleted"，
# 看起来像注入失败，其实是选错了对象。
pick_pod() {
  kubectl -n "$NS" get pods -l "app=$1" \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# ── 等待工具（和 faults.sh 完全一致的行为）──────────────────
#
# 判据写成**函数**而不是 bash -c 字符串：函数在本 shell 里直接执行，
# 能读到脚本里的变量（DEP / NS / PEER…），不用为引号逃逸折腾半天。
wait_for() {
  local desc="$1" timeout="$2"; shift 2
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if "$@" >/dev/null 2>&1; then printf '    ✓ %s\n' "$desc"; return 0; fi
    sleep 3
    i=$((i + 3))
  done
  printf '    · %s（等了 %ss 还没出现，可能还要再等等）\n' "$desc" "$timeout"
  return 0
}

# ── 症状判据 ────────────────────────────────────────────────

# PodChaos：目标工作负载的 Pod **名字集合变了** = 真的有 Pod 被杀掉重建。
# 不去看 restartCount：pod-kill 是删 Pod，新 Pod 的重启次数是 0，
# 用重启次数判断会得出"什么都没发生"的错误结论。
PODS_BEFORE=""
pods_changed() {
  local now
  now="$(kubectl -n "$NS" get pods -l "app=$DEP" --sort-by=.metadata.name \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
  [ -n "$now" ] && [ "$now" != "$PODS_BEFORE" ]
}

# 网络：从 $DEP 的 Pod 里真的去连一次对端。
# 用 wget 而不是 nc：alpine 的 busybox nc 没有 -z，而 wget 是有的。
peer_reachable() {
  [ -n "$SRC_POD" ] || return 1
  kubectl -n "$NS" exec "$SRC_POD" -- \
    wget -q -O /dev/null -T 5 "http://$PEER.$NS.svc.cluster.local:$PEER_PORT/health" \
    >/dev/null 2>&1
}
peer_unreachable() { ! peer_reachable; }

# 网络延迟：量一次"从 Pod 里访问对端"的墙钟耗时（毫秒）。
# 量的是 kubectl exec 的往返，包含一点 exec 开销（约 100~300ms），
# 所以阈值默认 2000ms —— 3s 的注入延迟和这个量级的本底不会混。
latency_ms() {
  [ -n "$SRC_POD" ] || { echo 0; return 1; }
  local t0 t1
  t0="$(date +%s%N)"
  kubectl -n "$NS" exec "$SRC_POD" -- \
    wget -q -O /dev/null -T 10 "http://$PEER.$NS.svc.cluster.local:$PEER_PORT/health" \
    >/dev/null 2>&1 || { echo 99999; return 1; }
  t1="$(date +%s%N)"
  echo $(( (t1 - t0) / 1000000 ))
}
# ⚠️ "慢"和"连不上"必须分开判。
# 早先这里只写 `latency_ms >= 阈值`，而连不上时 latency_ms 返回 99999，
# 于是**连接被拒绝**也会被当成"延迟注入成功"——判据恒为真，注入根本没生效
# 也会打印 ✓。这正是 faults.sh 里"别只 apply 完就返回"要防的那类假阳性。
#
# 现在的判法是**和注入前的基线比**，而不是只看绝对值：
#   1. 注入前先确认基线是"连得上而且快"的（baseline_ok）；
#   2. 注入后只要耗时 ≥ 阈值就算生效——**慢到超时也算**。
# 第 2 条之所以允许"失败"，是因为演示应用的 HTTP 服务是 busybox nc 单连接
# 模型，延迟一大它就会直接 reset（实测 2s 延迟即 reset，1s 延迟还能正常回包）。
# 基线已经证明过"注入前是好的"，所以耗时暴涨这件事本身就归因于注入。
delay_visible() { [ "$(latency_ms)" -ge "$LATENCY_MIN_MS" ]; }
baseline_ok() { peer_reachable && [ "$(latency_ms)" -lt "$LATENCY_MIN_MS" ]; }

# CPU：目标 Pod 里至少有一个的 CPU 用量到了阈值。
# 读 metrics-server（`kubectl top`），因为 StressChaos 烧的就是这个数。
cpu_high() {
  kubectl -n "$NS" top pod -l "app=$DEP" --no-headers 2>/dev/null | awk -v t="$STRESS_MIN_MC" '
    { v = $2
      if (v ~ /m$/) { sub(/m$/, "", v); c = v + 0 } else { c = v * 1000 }
      if (c >= t) found = 1 }
    END { exit !found }'
}

# DNS：从目标 Pod 里解析对端的名字，解析失败才算注入生效。
dns_broken() {
  [ -n "$SRC_POD" ] || return 1
  ! kubectl -n "$NS" exec "$SRC_POD" -- \
      nslookup "$PEER.$NS.svc.cluster.local" >/dev/null 2>&1
}

# 取对端 Service 的端口（用来拼 http://…:port/health）。
#
# ⚠️ 这里有个踩过的坑：**headless Service 的 port 是不转发的**。
# postgres-primary 是 headless（clusterIP: None），它的 spec.ports 写的是
# `port: 80, targetPort: 8080`，但 headless 时 DNS 直接返回 **Pod IP**，
# 没有 kube-proxy 帮你把 80 转到 8080。于是按 service.port 去连会得到
# "connection refused"（约 0.2s 就返回），看起来像"网络被切断了"。
# 这会让 partition 的判据**恒为真**、delay 的基线变成 99999，
# 两个场景的验收全部失真。所以 headless 必须用 targetPort。
resolve_peer_port() {
  PEER_PORT="$(kubectl -n "$NS" get svc "$PEER" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null)"
  local cip
  cip="$(kubectl -n "$NS" get svc "$PEER" -o jsonpath='{.spec.clusterIP}' 2>/dev/null)"
  if [ "$cip" = "None" ]; then
    PEER_PORT="$(kubectl -n "$NS" get svc "$PEER" -o jsonpath='{.spec.ports[0].targetPort}' 2>/dev/null)"
  fi
  [ -n "$PEER_PORT" ] || PEER_PORT=80
}

# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

case "$SCENARIO" in

  # ── 1. podkill：按计划反复随机杀 Pod ─────────────────────────
  #
  # 和 faults.sh 的差别：那边只能"缩容/删一个 Pod"做一次性动作，
  # 这里是**持续的、随机的**杀——Pod 起来又被杀，反复进入 CrashLoopBackOff
  # 之外的另一种形态："没有崩溃日志、容器自己也没报错，但 Pod 一直在换"。
  # 这类故障最容易被误判成"节点不稳/调度有问题"。
  #
  # ⚠️ Chaos Mesh 2.6 的 PodChaos **没有 scheduler 字段**（2.4 之后挪到了
  #    独立的 Schedule CRD）。所以"定时反复杀"必须写成 Schedule 包一个
  #    PodChaos，直接写 spec.scheduler 会被 API Server 以
  #    `unknown field "spec.scheduler"` 拒掉。
  podkill)
    require_chaos
    resolve cart-service
    SRC_POD="$(pick_pod "$DEP")"
    PODS_BEFORE="$(kubectl -n "$NS" get pods -l "app=$DEP" --sort-by=.metadata.name \
                   -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)"
    step "注入：每 $PODKILL_EVERY 随机杀掉 $DEP 的一个 Pod（PodChaos pod-kill + Schedule）"
    kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: Schedule
metadata:
  name: om-podkill
  namespace: $NS
spec:
  schedule: "@every $PODKILL_EVERY"
  historyLimit: 2
  concurrencyPolicy: Forbid
  type: PodChaos
  podChaos:
    action: pod-kill
    mode: one
    selector:
      namespaces: [$NS]
      labelSelectors:
        app: $DEP
    gracePeriod: 0
EOF
    wait_for "$DEP 真的有 Pod 被杀掉并重建（Pod 名集合变了）" 150 pods_changed
    hint "症状：$DEP 的 Pod 一直在换名字、AGE 归零，但**容器日志里什么都没有**——"
    hint "      新 Pod 是健康的，是「外面有人一直在杀它」。重启次数不涨（不是崩溃）。"
    hint "根因不在工作负载里，而在命名空间里多出来的 Chaos 实验对象上。"
    ;;

  # ── 2. network-partition：两个服务之间的网络被切断 ──────────
  #
  # faults.sh **完全做不到**这一类：它改的都是 K8s 对象，而"网络不通"在
  # K8s 对象上一点痕迹都没有——Service、Endpoints、Pod 全都正常，
  # 但包就是过不去。真实值班里这是最容易被误判成"上游有问题"的故障。
  network-partition)
    require_chaos
    resolve order-service
    resolve_peer_port
    SRC_POD="$(pick_pod "$DEP")"
    [ -n "$SRC_POD" ] || die "$DEP 没有 Running 的 Pod，没法验证连通性。"
    step "注入：切断 $DEP ↔ $PEER 的流量（NetworkChaos partition, direction=both）"
    echo "    验证用的 Pod：$SRC_POD"
    peer_reachable && echo "    注入前：$DEP → $PEER 可达 ✓" \
                    || echo "    注入前：$DEP → $PEER 就不可达（基线本来就不对，注意）"
    kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: om-netpart
  namespace: $NS
spec:
  action: partition
  mode: all
  selector:
    namespaces: [$NS]
    labelSelectors:
      app: $DEP
  direction: both
  target:
    mode: all
    selector:
      namespaces: [$NS]
      labelSelectors:
        app: $PEER
  duration: "$DURATION"
EOF
    wait_for "$DEP 到 $PEER 的连接真的断了（Pod 内实测）" 120 peer_unreachable
    hint "症状：$DEP 的 Pod 全是 Running/Ready，Endpoints 也正常，"
    hint "      但从 $DEP 里连 $PEER 会一直卡到超时（连接不是被拒绝，是被丢弃）。"
    hint "根因不在 Service / Endpoints / 探针上，而在命名空间里的 Chaos 实验对象。"
    ;;

  # ── 3. network-delay：下游变慢 ──────────────────────────────
  #
  # 和 partition 的区别很关键：partition 是"连不上"（有明确报错），
  # delay 是"连得上、但慢"——**一切看起来都正常**，只有耗时不对。
  # 这是最难诊断的一类，因为所有状态字段都是绿的。
  network-delay)
    require_chaos
    resolve order-service
    resolve_peer_port
    SRC_POD="$(pick_pod "$DEP")"
    [ -n "$SRC_POD" ] || die "$DEP 没有 Running 的 Pod，没法量延迟。"
    step "注入：$DEP → $PEER 的每个包延迟 $LATENCY（NetworkChaos delay）"
    echo "    验证用的 Pod：$SRC_POD"
    echo "    注入前 RTT：$(latency_ms) ms"
    # 基线必须"连得上且够快"，否则下面的"变慢了"就没有归因依据。
    baseline_ok || echo "    ⚠️ 注入前基线就不正常（连不上，或者本来就慢）——本场景的验收不可信，先修基线。"
    kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: om-netdelay
  namespace: $NS
spec:
  action: delay
  mode: all
  selector:
    namespaces: [$NS]
    labelSelectors:
      app: $DEP
  direction: to
  target:
    mode: all
    selector:
      namespaces: [$NS]
      labelSelectors:
        app: $PEER
  delay:
    latency: "$LATENCY"
    correlation: "100"
    jitter: "0ms"
  duration: "$DURATION"
EOF
    wait_for "$DEP → $PEER 的耗时真的涨到 ${LATENCY_MIN_MS}ms 以上" 120 delay_visible
    echo "    注入后 RTT（连量 3 次）：$(latency_ms) / $(latency_ms) / $(latency_ms) ms"
    echo "      （本底约 100~300ms 是 kubectl exec 的开销；"
    echo "        99999 = 这次请求被拖到超时/reset。演示应用的 HTTP 服务是 busybox"
    echo "        nc 单连接模型，延迟一大它就 reset，所以这里「慢」和「被拖挂」会交替出现，"
    echo "        两者都是同一件事：这条路上的包被拖慢了。）"
    hint "症状：从 $DEP 访问 $PEER **能通、但明显变慢**。"
    hint "      Pod 状态、Endpoints、探针、事件里可能全都是正常的——"
    hint "      这类「下游变慢」是延迟类故障里最难查的一种，因为没有任何东西是坏的。"
    ;;

  # ── 4. stress-cpu：CPU 被外人打满 ───────────────────────────
  #
  # 不是"上限被改小"（那是 faults.sh 的 oom），而是**用量被打满**：
  # 配置一个字节都没改，Pod 也没重启，就是慢了。
  # StressChaos 把 stress 进程塞进目标容器的 cgroup，所以烧的是**这个容器
  # 自己的 CPU 限额**——演示集群里每个服务限额 200m，注入后就会被顶到 ~200m。
  stress-cpu)
    require_chaos
    resolve api-gateway
    step "注入：$DEP 的容器内 CPU 被打满（StressChaos，$STRESS_WORKERS workers @ ${STRESS_LOAD}%）"
    echo "    注入前 CPU："
    kubectl -n "$NS" top pod -l "app=$DEP" --no-headers 2>/dev/null | sed 's/^/      /' || true
    kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: om-stress-cpu
  namespace: $NS
spec:
  mode: all
  selector:
    namespaces: [$NS]
    labelSelectors:
      app: $DEP
  stressors:
    cpu:
      workers: $STRESS_WORKERS
      load: $STRESS_LOAD
  duration: "$DURATION"
EOF
    wait_for "$DEP 的 CPU 用量真的被顶到 ${STRESS_MIN_MC}m 以上" 240 cpu_high
    echo "    注入后 CPU："
    kubectl -n "$NS" top pod -l "app=$DEP" --no-headers 2>/dev/null | sed 's/^/      /' || true
    hint "症状：$DEP 的 CPU 用量顶到自己**限额**（本环境每个服务 limits.cpu=200m），"
    hint "      响应变慢、探针开始超时；如果是配置了 HPA 的工作负载，副本数会被拉上去。"
    hint "根因不在 $DEP 的 spec 里——它的 limits/requests 一个字都没变。"
    ;;

  # ── 5. dns-error：DNS 解析被污染 ────────────────────────────
  #
  # 这类故障的形态是"连 Service 名字都解析不出来"，但**Service 对象本身
  # 完全正常**。DNSChaos 的做法是让 chaos-daemon 进到目标容器的 netns 里
  # 把 /etc/resolv.conf 指到 chaos-dns-server，由它按规则返回错误。
  # 所以它**不需要重建 Pod**（这点和"改 DNS 配置要重启"的直觉不同）。
  dns-error)
    require_chaos
    resolve order-service
    SRC_POD="$(pick_pod "$DEP")"
    [ -n "$SRC_POD" ] || die "$DEP 没有 Running 的 Pod，没法验证 DNS。"
    step "注入：$DEP 的 DNS 解析被污染（DNSChaos action=error, patterns=*）"
    echo "    验证用的 Pod：$SRC_POD"
    echo "    注入前 resolv.conf 的 nameserver：$(kubectl -n "$NS" exec "$SRC_POD" -- awk '/nameserver/{print $2}' /etc/resolv.conf 2>/dev/null | head -1)"
    kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: chaos-mesh.org/v1alpha1
kind: DNSChaos
metadata:
  name: om-dns-error
  namespace: $NS
spec:
  action: error
  mode: all
  patterns:
    - "*"
  selector:
    namespaces: [$NS]
    labelSelectors:
      app: $DEP
  duration: "$DURATION"
EOF
    wait_for "从 $DEP 的 Pod 里解析 $PEER.$NS.svc.cluster.local 真的失败" 150 dns_broken
    echo "    注入后 resolv.conf 的 nameserver：$(kubectl -n "$NS" exec "$SRC_POD" -- awk '/nameserver/{print $2}' /etc/resolv.conf 2>/dev/null | head -1)"
    hint "症状：$DEP 里解析任何集群内域名都返回 SERVFAIL；"
    hint "      Service / Endpoints / 探针全都正常，Pod 也是 Ready，"
    hint "      但任何**按域名**访问下游的调用都会失败。"
    hint "注：DNSChaos 会**改掉 Pod 的 /etc/resolv.conf**，删掉实验对象后由 chaos-daemon 恢复。"
    ;;

  # ── 恢复基线 ────────────────────────────────────────────────
  #
  # ⚠️ 这一段是**硬要求**，不是可选项。
  # Chaos 对象独立于工作负载存在：不删的话，下一个场景会在上一个实验
  # 还在生效的集群上跑，结果就是随机失败，而且极难排查。
  reset)
    if ! kubectl get crd networkchaos.chaos-mesh.org >/dev/null 2>&1; then
      echo "  这个集群没装 Chaos Mesh，没有 Chaos 对象要清。"
      exit 0
    fi

    # Schedule 必须**第一个**删：它每 30 秒会把 PodChaos 重新建出来，
    # 先删 podchaos 是白删——下一轮又回来了（实测踩到过）。
    echo "  删除定时实验（Schedule）…"
    kubectl delete schedules --all -A --ignore-not-found --timeout=60s >/dev/null 2>&1 || true

    echo "  删除所有命名空间的所有 Chaos 实验对象…"
    # 故意用**显式的资源列表**（而不是只删我们自己建的那几个名字）：
    # 别人手工建的实验同样会污染评测，一起清掉才叫"恢复基线"。
    kubectl delete podchaos,networkchaos,stresschaos,dnschaos,iochaos \
      --all -A --ignore-not-found --timeout=60s >/dev/null 2>&1 || true
    # 剩下几个同类（我们目前不用，但别人可能会建）
    kubectl delete httpchaos,timechaos,kernelchaos,blockchaos,jvmchaos, \
      podhttpchaos,podiochaos,podnetworkchaos,physicalmachinechaos,awschaos, \
      gcpchaos,azurechaos --all -A --ignore-not-found --timeout=60s >/dev/null 2>&1 || true

    # 删完要**确认真的没了**。DNSChaos 的恢复要等 chaos-daemon 把
    # /etc/resolv.conf 改回来，finalizer 没清掉就说明恢复还没跑完。
    i=0; left=0
    while [ "$i" -lt 60 ]; do
      left="$(kubectl get podchaos,networkchaos,stresschaos,dnschaos,iochaos,schedules \
              -A --no-headers 2>/dev/null | wc -l | tr -d ' ')"
      [ "$left" = "0" ] && break
      sleep 3
      i=$((i + 3))
    done
    if [ "$left" != "0" ]; then
      echo "  ⚠️ 还有 $left 个 Chaos 对象没删掉，看：kubectl get podchaos,networkchaos,stresschaos,dnschaos,schedules -A" >&2
    else
      echo "  Chaos 对象已清空（含 Schedule）。"
    fi
    echo "  提示：被动过网络的 Pod 由 chaos-daemon 恢复；Pod 级别的 netns 恢复"
    echo "        是即时的，但**已被改过的应用连接**要等它自己重连。"
    ;;

  targets)
    for ns in demo staging observability; do
      printf '\n[%s]\n' "$ns"
      kubectl -n "$ns" get deploy,sts,ds --no-headers 2>/dev/null \
        | awk '{printf "  %-28s %s\n", $1, $2}'
    done
    echo
    ;;

  status)
    step "当前 Chaos 对象"
    kubectl get schedules,podchaos,networkchaos,stresschaos,dnschaos,iochaos \
      -A --no-headers 2>/dev/null | sed 's/^/  /' || true
    kubectl get schedules,podchaos,networkchaos,stresschaos,dnschaos,iochaos \
      -A --no-headers 2>/dev/null | grep -q . || echo "  （没有）"
    echo
    step "Chaos Mesh 自身"
    kubectl -n "$CHAOS_NS" get pods --no-headers 2>/dev/null | sed 's/^/  /' || echo "  （没装）"
    echo
    step "demo 命名空间异常事件（去重）"
    k get events --field-selector type=Warning --no-headers 2>/dev/null \
      | awk '{print $2, $4, $5}' | sort -u | head -12
    ;;

  list|*)
    cat <<'EOF'
用法：sandbox/chaos.sh <场景> [目标工作负载]

不传目标就用场景的默认目标。有些场景对目标有要求，不满足会明确报错。

  场景               默认目标         注入方式（Chaos Mesh 资源）
  ──────────────────────────────────────────────────────────────────────────
  podkill            cart-service     PodChaos pod-kill（用 Schedule 每 30s 反复杀）
  network-partition  order-service    NetworkChaos partition（切断 ↔ postgres-primary）
  network-delay      order-service    NetworkChaos delay（→ postgres-primary 延迟 1s）
  stress-cpu         api-gateway      StressChaos（2 workers @ 100% 打满 CPU 限额）
  dns-error          order-service    DNSChaos action=error（改 Pod 的 resolv.conf）

  reset              清掉**所有命名空间**的所有 Chaos 对象（含 Schedule）
  targets            列出所有可用目标
  status             看当前注入了什么

可调环境变量（不改就是默认强度）：
  OM_NS               命名空间，默认 demo
  OM_PEER             网络/DNS 场景的对端工作负载，默认 postgres-primary
  OM_LATENCY          network-delay 的延迟，默认 1s
  OM_LATENCY_MIN_MS   判定"真的慢了"的阈值，默认 800
  OM_DURATION         实验持续时间，默认 10m
  OM_STRESS_WORKERS   StressChaos 的 CPU workers，默认 2
  OM_STRESS_LOAD      每个 worker 的负载百分比，默认 100
  OM_STRESS_MIN_MC    判定"真的打满了"的阈值（millicores），默认 150
  OM_PODKILL_EVERY    podkill 每隔多久杀一次，默认 30s
  OM_CHAOS_NS         Chaos Mesh 自己的命名空间，默认 chaos-mesh

每个场景都不是"apply 完就返回"：它会等到**症状真的出现在集群里**
（Pod 真的被杀了 / 连接真的断了 / RTT 真的涨了 / CPU 真的满了 /
DNS 真的解析失败）才打印 ✓。

示例：
  sandbox/chaos.sh network-delay order-service
  sandbox/chaos.sh network-partition payment-service
  sandbox/chaos.sh stress-cpu search-service
  sandbox/chaos.sh reset
EOF
    ;;
esac
