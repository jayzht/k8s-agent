#!/usr/bin/env bash
# 故障注入脚本 —— 站在"开发者推了一次坏变更"的视角。
#
# 这些操作**不是** Agent 做的，而是模拟真实世界里 Agent 需要去诊断的那些故障。
# 用法：sandbox/faults.sh {oom|crash|image|pending|protected|reset|status}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$ROOT/bin:$PATH"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
NS=demo
DEP=api-gateway

k() { kubectl -n "$NS" "$@"; }

case "${1:-status}" in
  # --- OOMKilled：内存 limit 压到 64Mi，容器申请 256MB ---
  oom)
    echo "==> 注入故障：OOMKilled"
    k patch deployment "$DEP" --type=strategic -p '{
      "spec":{"template":{"spec":{"containers":[{
        "name":"app",
        "env":[{"name":"MODE","value":"oom"},{"name":"OOM_MB","value":"50"}],
        "resources":{"requests":{"cpu":"50m","memory":"32Mi"},
                     "limits":{"cpu":"200m","memory":"64Mi"}}
      }]}}}}'
    ;;

  # --- CrashLoopBackOff：容器启动即退出 ---
  crash)
    echo "==> 注入故障：CrashLoopBackOff"
    k patch deployment "$DEP" --type=strategic -p '{
      "spec":{"template":{"spec":{"containers":[{
        "name":"app",
        "env":[{"name":"MODE","value":"crash"}]
      }]}}}}'
    ;;

  # --- ImagePullBackOff：引用一个不存在的镜像 tag ---
  image)
    echo "==> 注入故障：ImagePullBackOff"
    k patch deployment "$DEP" --type=strategic -p '{
      "spec":{"template":{"spec":{"containers":[{
        "name":"app",
        "image":"om-demo-app:does-not-exist",
        "imagePullPolicy":"IfNotPresent"
      }]}}}}'
    ;;

  # --- Pending：不可满足的 nodeSelector ---
  pending)
    echo "==> 注入故障：Pending（不可调度）"
    k patch deployment "$DEP" --type=strategic -p '{
      "spec":{"template":{"spec":{"nodeSelector":{"disktype":"ssd-does-not-exist"}}}}}'
    ;;

  # --- 保护标签熔断：不改负载，只演示 Agent 拒绝操作 billing-core ---
  protected)
    echo "==> billing-core 带有 omagent.io/protected=true 标签"
    k get deployment billing-core -o jsonpath='{.metadata.labels}'; echo
    echo "    对它的任何 T1 及以上操作都会被熔断规则拦截。"
    ;;

  # --- 恢复基线 ---
  #
  # ⚠️ **不能只靠 kubectl apply**。`apply` 只会移除"上次 apply 记录过"的字段，
  # 而故障是用 `kubectl patch` 注入的——patch 改的字段不在 last-applied 注解里，
  # apply 不会清掉它们。结果就是 nodeSelector 永远留在 Deployment 上，
  # 表现为"点了恢复基线，Pod 还是 Pending"（真实用户报上来的问题）。
  # 因此先用显式 patch 把注入过的字段清干净，再 apply 基线。
  reset)
    echo "==> 恢复基线"
    k patch deployment "$DEP" --type=strategic -p '{
      "spec":{"template":{"spec":{
        "nodeSelector":null,
        "containers":[{
          "name":"app",
          "image":"om-demo-app:1",
          "imagePullPolicy":"Never",
          "env":[{"name":"MODE","value":"normal"}],
          "resources":{"requests":{"cpu":"50m","memory":"32Mi"},
                       "limits":{"cpu":"200m","memory":"128Mi"}}
        }]
      }}}}' >/dev/null 2>&1 || true
    kubectl apply -f "$ROOT/sandbox/app.yaml" >/dev/null
    k rollout restart deployment "$DEP" >/dev/null
    k rollout status deployment "$DEP" --timeout=180s || true
    k rollout status statefulset session-store --timeout=180s || true
    ;;

  status)
    echo "==> 当前状态"
    k get pods -o wide
    ;;

  *)
    echo "用法: $0 {oom|crash|image|pending|protected|reset|status}" >&2
    exit 2
    ;;
esac

if [ "${1:-}" != "status" ] && [ "${1:-}" != "reset" ] && [ "${1:-}" != "protected" ]; then
  echo "--- 等待故障显现（最多 90s）---"
  sleep 20
  k get pods -o wide || true
fi
