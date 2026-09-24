#!/usr/bin/env python3
"""生成沙箱集群的清单文件。

为什么用生成器而不是手写 1000 行 YAML：
25 个工作负载的 Deployment/Service/PDB/HPA 结构高度重复，手写必然出现
"api-gateway 有 PDB、order-service 忘了加" 这类漂移。拓扑在这里是一张表，
清单是它的投影——**改表，不改编出来的文件**。

    python sandbox/topology.py            # 写入 sandbox/manifests/
    python sandbox/topology.py --print    # 打到标准输出
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "sandbox" / "manifests"

IMAGE = "om-demo-app:1"
APP_PORT = 8080


@dataclass
class Svc:
    """一个工作负载的完整声明。"""

    name: str
    ns: str = "demo"
    kind: str = "deployment"  # deployment | statefulset | daemonset
    replicas: int = 2
    svc_port: int = 80
    target_port: int = APP_PORT
    cpu_req: str = "50m"
    cpu_lim: str = "200m"
    mem_req: str = "32Mi"
    mem_lim: str = "128Mi"
    pdb_min: int | None = None
    hpa: tuple[int, int] | None = None
    expose: bool = True
    pvc: str | None = None
    configmap: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    node_pool: str | None = None
    note: str = ""


# ---------------------------------------------------------------------------
# 拓扑表 —— 一个电商/订单系统的完整切面
#
# 分层的意义：故障**几乎从来不是单点**的。order-service 重启没反应，
# 原因可能在它下游的 postgres；payment-service 报错，根因可能是 kafka。
# 场景里必须有这些依赖关系，诊断才有"顺着链路查下去"的空间，
# 而不是"看到 CrashLoop 就说重启"。
# ---------------------------------------------------------------------------

DEMO: list[Svc] = [
    # ── 接入层 ──────────────────────────────────────────────
    Svc("web-frontend", replicas=3, note="用户入口，挂着 Ingress"),
    Svc("api-gateway", replicas=3, pdb_min=2, hpa=(3, 8),
        note="网关：有 PDB，滚动/驱逐时能看出约束"),

    # ── 业务服务 ────────────────────────────────────────────
    Svc("order-service", replicas=3, configmap="order-service-config",
        note="订单：配置来自 ConfigMap，配置写错要靠改 ConfigMap + 重启"),
    Svc("cart-service", replicas=2),
    Svc("payment-service", replicas=2, configmap="payment-service-config"),
    Svc("inventory-service", replicas=2),
    Svc("user-service", replicas=2),
    Svc("search-service", replicas=2),
    Svc("notification-service", replicas=2),
    Svc("recommendation-service", replicas=2),
    Svc("auth-service", replicas=2, configmap="auth-service-config",
        note="认证：所有服务的上游，它挂了会引发一大片连锁报错"),

    # ── 异步与批处理 ────────────────────────────────────────
    Svc("order-worker", replicas=2),
    Svc("reporting-worker", replicas=1, node_pool="batch",
        note="报表：只调度到 batch 节点池"),

    # ── 有状态 ──────────────────────────────────────────────
    Svc("postgres-primary", kind="statefulset", replicas=1, pvc="256Mi",
        note="主库：单点有状态，重启影响面最大"),
    Svc("redis-cache", kind="statefulset", replicas=1, pvc="64Mi"),
    Svc("session-store", kind="statefulset", replicas=1, pvc="64Mi"),
    Svc("kafka-broker", kind="statefulset", replicas=1, pvc="512Mi"),
]

STAGING: list[Svc] = [
    Svc("web-frontend", ns="staging", replicas=1),
    Svc("api-gateway", ns="staging", replicas=1),
    Svc("order-service", ns="staging", replicas=1),
    Svc("feature-flags", ns="staging", replicas=1),
    Svc("postgres-primary", ns="staging", kind="statefulset", replicas=1, pvc="128Mi"),
]

OBSERVABILITY: list[Svc] = [
    Svc("prometheus", ns="observability", kind="statefulset", replicas=1, pvc="512Mi"),
    Svc("grafana", ns="observability", replicas=1),
    # DaemonSet：每个 worker 上一个。drain 节点时它必须被跳过——
    # 这正是 kubectl drain 的经典语义，也是"排空节点"演示里最容易做错的地方。
    Svc("log-collector", ns="observability", kind="daemonset", expose=False,
        cpu_req="20m", cpu_lim="100m", mem_req="24Mi", mem_lim="96Mi",
        note="DaemonSet：4 个 worker 各一个副本"),
]

ALL = DEMO + STAGING + OBSERVABILITY

CONFIGMAPS: dict[str, dict[str, dict[str, str]]] = {
    "demo": {
        "order-service-config": {
            "DB_HOST": "postgres-primary.demo.svc.cluster.local",
            "DB_PORT": "5432",
            "CACHE_URL": "redis-cache.demo.svc.cluster.local:6379",
            "QUEUE_URL": "kafka-broker.demo.svc.cluster.local:9092",
        },
        "payment-service-config": {
            "GATEWAY_URL": "https://pay.internal.example.com",
            "SETTLEMENT_CRON": "0 2 * * *",
        },
        "auth-service-config": {
            "TOKEN_TTL": "3600",
            "JWKS_URL": "http://auth-service.demo.svc.cluster.local:8080/.well-known/jwks.json",
            "SESSION_BACKEND": "session-store.demo.svc.cluster.local:6379",
        },
    },
    "staging": {
        "feature-flags-config": {
            "FLAGS_ENDPOINT": "http://feature-flags.staging.svc.cluster.local:8080",
            "REFRESH_INTERVAL": "30s",
        },
    },
}

INGRESS_HOST = "shop.demo.local"

# ⚠️ 标签值必须是 ASCII（`(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?`），
# 中文只能放注解。踩过一次：把「业务演示」写进 label，kubectl apply 直接报 Invalid。
NAMESPACES: dict[str, dict] = {
    "demo": {"labels": {"omagent.io/managed": "true"}, "note": "业务演示：主系统"},
    "staging": {"labels": {"omagent.io/managed": "true"}, "note": "预发：配额收紧"},
    "observability": {"labels": {"omagent.io/managed": "true"}, "note": "监控组件"},
}


# ---------------------------------------------------------------------------
# YAML 生成
# ---------------------------------------------------------------------------


def _labels(s: Svc) -> str:
    return f"        app: {s.name}\n        app.kubernetes.io/name: {s.name}"


def _env_block(s: Svc) -> str:
    out = [
        "          env:",
        "            - name: MODE",
        f'              value: "{s.env.get("MODE", "normal")}"',
        "            - name: SERVICE_NAME",
        f'              value: "{s.name}"',
        "            - name: APP_ENV",
        f'              value: "{s.ns}"',
    ]
    for k, v in s.env.items():
        if k == "MODE":
            continue
        out.append(f"            - name: {k}")
        out.append(f'              value: "{v}"')
    if s.configmap:
        out.append("          envFrom:")
        out.append("            - configMapRef:")
        out.append(f"                name: {s.configmap}")
    return "\n".join(out)


def _resources(s: Svc) -> str:
    return (
        "          resources:\n"
        "            requests:\n"
        f"              cpu: {s.cpu_req}\n"
        f"              memory: {s.mem_req}\n"
        "            limits:\n"
        f"              cpu: {s.cpu_lim}\n"
        f"              memory: {s.mem_lim}"
    )


def _probes() -> str:
    return (
        "          readinessProbe:\n"
        "            httpGet:\n"
        "              path: /health\n"
        f"              port: {APP_PORT}\n"
        "            initialDelaySeconds: 2\n"
        "            periodSeconds: 3\n"
        "            failureThreshold: 3\n"
        "          livenessProbe:\n"
        "            httpGet:\n"
        "              path: /health\n"
        f"              port: {APP_PORT}\n"
        "            initialDelaySeconds: 5\n"
        "            periodSeconds: 5\n"
        "            failureThreshold: 3"
    )


def _pod_spec(s: Svc, indent: str = "      ") -> str:
    parts = [f"{indent}containers:",
             f"{indent}  - name: app",
             f"{indent}    image: {IMAGE}",
             f"{indent}    imagePullPolicy: Never",
             f"{indent}    ports:",
             f"{indent}      - containerPort: {APP_PORT}"]
    parts.append(_env_block(s).replace("          ", indent + "    "))
    parts.append(_resources(s).replace("          ", indent + "    "))
    parts.append(_probes().replace("          ", indent + "    "))
    if s.node_pool:
        parts.append(f"{indent}nodeSelector:\n{indent}  node-pool: {s.node_pool}")
    return "\n".join(parts)


def _meta(s: Svc, extra: str = "") -> str:
    return (
        f"metadata:\n"
        f"  name: {s.name}\n"
        f"  namespace: {s.ns}\n"
        f"  labels:\n"
        f"    app: {s.name}\n"
        f"    app.kubernetes.io/name: {s.name}\n"
        f"    app.kubernetes.io/part-of: demo-shop\n"
        + extra
    )


def _meta_annotated(s: Svc) -> str:
    """带说明注解的 metadata。注解只是给人看的，不参与任何逻辑。"""
    if not s.note:
        return _meta(s)
    extra = '  annotations:\n    omagent.io/note: "' + s.note + '"\n'
    return _meta(s, extra)


def deployment(s: Svc) -> str:
    return (
        f"apiVersion: apps/v1\nkind: Deployment\n"
        f"{_meta_annotated(s)}"
        f"spec:\n"
        f"  replicas: {s.replicas}\n"
        f"  selector:\n    matchLabels:\n      app: {s.name}\n"
        f"  strategy:\n    type: RollingUpdate\n    rollingUpdate:\n"
        f"      maxUnavailable: 1\n      maxSurge: 1\n"
        f"  template:\n"
        f"    metadata:\n      labels:\n{_labels(s)}\n"
        f"    spec:\n{_pod_spec(s)}\n"
    )


def statefulset(s: Svc) -> str:
    vct = ""
    if s.pvc:
        vct = (
            "  volumeClaimTemplates:\n"
            "    - metadata:\n        name: data\n"
            "      spec:\n"
            "        accessModes: [\"ReadWriteOnce\"]\n"
            "        resources:\n          requests:\n"
            f"            storage: {s.pvc}\n"
        )
    return (
        f"apiVersion: apps/v1\nkind: StatefulSet\n"
        f"{_meta(s)}"
        f"spec:\n"
        f"  serviceName: {s.name}\n"
        f"  replicas: {s.replicas}\n"
        f"  selector:\n    matchLabels:\n      app: {s.name}\n"
        f"  template:\n"
        f"    metadata:\n      labels:\n{_labels(s)}\n"
        f"    spec:\n{_pod_spec(s)}\n"
        f"{vct}"
    )


def daemonset(s: Svc) -> str:
    return (
        f"apiVersion: apps/v1\nkind: DaemonSet\n"
        f"{_meta(s)}"
        f"spec:\n"
        f"  selector:\n    matchLabels:\n      app: {s.name}\n"
        f"  template:\n"
        f"    metadata:\n      labels:\n{_labels(s)}\n"
        f"    spec:\n{_pod_spec(s)}\n"
    )


def service(s: Svc) -> str:
    headless = "  clusterIP: None\n" if s.kind == "statefulset" else ""
    return (
        f"apiVersion: v1\nkind: Service\n"
        f"metadata:\n  name: {s.name}\n  namespace: {s.ns}\n"
        f"  labels:\n    app: {s.name}\n"
        f"spec:\n{headless}"
        f"  selector:\n    app: {s.name}\n"
        f"  ports:\n    - name: http\n      port: {s.svc_port}\n"
        f"      targetPort: {s.target_port}\n"
    )


def pdb(s: Svc) -> str:
    return (
        f"apiVersion: policy/v1\nkind: PodDisruptionBudget\n"
        f"metadata:\n  name: {s.name}-pdb\n  namespace: {s.ns}\n"
        f"spec:\n  minAvailable: {s.pdb_min}\n"
        f"  selector:\n    matchLabels:\n      app: {s.name}\n"
    )


def hpa(s: Svc) -> str:
    lo, hi = s.hpa  # type: ignore[misc]
    return (
        f"apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\n"
        f"metadata:\n  name: {s.name}\n  namespace: {s.ns}\n"
        f"spec:\n  scaleTargetRef:\n    apiVersion: apps/v1\n    kind: Deployment\n"
        f"    name: {s.name}\n  minReplicas: {lo}\n  maxReplicas: {hi}\n"
        f"  metrics:\n    - type: Resource\n      resource:\n        name: cpu\n"
        f"        target:\n          type: Utilization\n          averageUtilization: 70\n"
    )


def configmap(name: str, ns: str, data: dict[str, str]) -> str:
    lines = [f"apiVersion: v1\nkind: ConfigMap",
             f"metadata:\n  name: {name}\n  namespace: {ns}",
             "data:"]
    for k, v in data.items():
        lines.append(f'  {k}: "{v}"')
    return "\n".join(lines) + "\n"


def ingress() -> str:
    return (
        f"apiVersion: networking.k8s.io/v1\nkind: Ingress\n"
        f"metadata:\n  name: shop-ingress\n  namespace: demo\n"
        f"spec:\n  ingressClassName: nginx\n  rules:\n"
        f"    - host: {INGRESS_HOST}\n      http:\n        paths:\n"
        f"          - path: /\n            pathType: Prefix\n"
        f"            backend:\n              service:\n"
        f"                name: web-frontend\n                port:\n                  number: 80\n"
    )


def staging_quota() -> str:
    """staging 的配额刻意收紧：新增副本时会撞上限，这是真实的预发环境常态。"""
    return (
        "apiVersion: v1\nkind: ResourceQuota\n"
        "metadata:\n  name: staging-quota\n  namespace: staging\n"
        "spec:\n  hard:\n    requests.cpu: \"2\"\n    requests.memory: 4Gi\n"
        "    limits.cpu: \"4\"\n    limits.memory: 8Gi\n    pods: \"20\"\n"
        "---\n"
        "apiVersion: v1\nkind: LimitRange\n"
        "metadata:\n  name: staging-limits\n  namespace: staging\n"
        "spec:\n  limits:\n    - type: Container\n"
        "      default:\n        cpu: 200m\n        memory: 128Mi\n"
        "      defaultRequest:\n        cpu: 50m\n        memory: 32Mi\n"
        "      max:\n        cpu: \"1\"\n        memory: 1Gi\n"
    )


def ns_doc(name: str, spec: dict) -> str:
    lab = "\n".join(f'    {k}: "{v}"' for k, v in spec["labels"].items())
    note = spec.get("note", "")
    ann = f'  annotations:\n    omagent.io/note: "{note}"\n' if note else ""
    return (f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {name}\n"
            f"  labels:\n{lab}\n{ann}")


HEADER = (
    "# ⚠️ 本文件由 sandbox/topology.py 生成，不要手工修改。\n"
    "# 改拓扑请改 topology.py 里的表，然后重新运行：\n"
    "#     python sandbox/topology.py && kubectl apply -f sandbox/manifests/\n"
)


def render() -> dict[str, str]:
    by_ns: dict[str, list[Svc]] = {}
    for s in ALL:
        by_ns.setdefault(s.ns, []).append(s)

    files: dict[str, str] = {}

    files["00-namespaces.yaml"] = HEADER + "\n" + "\n---\n".join(
        ns_doc(n, lab) for n, lab in NAMESPACES.items()
    )

    builder = {"deployment": deployment, "statefulset": statefulset, "daemonset": daemonset}

    for ns, svcs in by_ns.items():
        docs: list[str] = []
        for s in svcs:
            docs.append(builder[s.kind](s))
            if s.expose:
                docs.append(service(s))
            if s.pdb_min is not None:
                docs.append(pdb(s))
            if s.hpa is not None:
                docs.append(hpa(s))

        # 命名空间里的配置
        for cm_name, data in CONFIGMAPS.get(ns, {}).items():
            docs.append(configmap(cm_name, ns, data))

        if ns == "demo":
            docs.append(ingress())
        if ns == "staging":
            docs.append(staging_quota())

        num = {"demo": "10", "staging": "20", "observability": "30"}.get(ns, "90")
        files[f"{num}-{ns}.yaml"] = HEADER + "\n" + "\n---\n".join(docs)

    return files


def main() -> int:
    files = render()
    if "--print" in sys.argv:
        for name, body in files.items():
            print(f"########## {name} ##########")
            print(body)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (OUT_DIR / name).write_text(body, encoding="utf-8")
        lines = body.count("\n")
        print(f"  {name:<26} {lines:>4} 行")

    n = len(ALL)
    by_kind: dict[str, int] = {}
    for s in ALL:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    print(f"\n共 {n} 个工作负载："
          + "、".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
    print(f"命名空间：{'、'.join(NAMESPACES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
