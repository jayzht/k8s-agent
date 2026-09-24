"""影响面分析（PRD 5.4）。

这是让用户"敢点确认"的核心信息，也是 L1 档位产品力的主要载体。

铁律：**每个字段都必须来自集群真实状态，不允许推测。**
推测出来的影响面比没有影响面更危险——它会给用户虚假的安全感。
取不到的值一律留空并显式标注"未知"，由确认卡片呈现给用户。
"""

from __future__ import annotations

from typing import Any

from .k8s import K8sClient, K8sUnavailable
from .models import Impact


def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """K8s 标签选择器语义：selector 是 labels 的子集即匹配。"""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _workload_labels(wl: Any) -> dict[str, str]:
    tmpl = getattr(wl.spec, "template", None)
    if tmpl is None:
        return {}
    return dict(tmpl.metadata.labels or {})


def _detect_pvc(wl: Any, kind: str) -> bool:
    """检测工作负载是否挂载持久卷。有状态 + PVC 是升级确认强度的关键信号。"""
    if kind.lower() == "statefulset":
        vct = getattr(wl.spec, "volume_claim_templates", None)
        if vct:
            return True
    tmpl = getattr(wl.spec, "template", None)
    if tmpl is None:
        return False
    for vol in tmpl.spec.volumes or []:
        if getattr(vol, "persistent_volume_claim", None) is not None:
            return True
    return False


def analyse_workload_impact(
    k8s: K8sClient,
    namespace: str,
    kind: str,
    name: str,
    target_replicas: int | None = None,
) -> Impact:
    """分析对某个工作负载执行变更的影响面。

    ``target_replicas`` 用于 scale 类操作：影响面必须按**改完之后**的状态算，
    而不是当前状态。否则"把 3 缩到 1"和"把 3 扩到 10"会给出同一张卡片，
    而前者会把一个有冗余的服务变成单点——这恰恰是操作员最需要看到的信息。
    """
    impact = Impact()
    try:
        wl = k8s.read_workload(namespace, kind, name)
    except Exception as exc:  # noqa: BLE001
        impact.notes.append(f"无法读取工作负载，影响面未知: {exc}")
        return impact

    current = int(wl.spec.replicas or 0)
    impact.replicas = current
    impact.stateful = kind.lower() == "statefulset"
    impact.has_pvc = _detect_pvc(wl, kind)

    if target_replicas is None:
        impact.single_point = current <= 1
    else:
        target_replicas = int(target_replicas)
        impact.target_replicas = target_replicas
        # 单点判断看**改完之后**
        impact.single_point = target_replicas <= 1
        if target_replicas < current:
            impact.pods_removed = current - target_replicas
            impact.notes.append(
                f"副本数将从 {current} 缩减到 {target_replicas}，"
                f"会终止 {impact.pods_removed} 个实例"
            )
            if target_replicas == 0:
                impact.notes.append("⚠️ 目标副本数为 0：这个服务将完全没有实例在跑")
            elif target_replicas == 1 and current > 1:
                impact.notes.append("⚠️ 缩到 1 个副本后就没有冗余了，该实例故障即服务中断")
        elif target_replicas > current:
            impact.pods_added = target_replicas - current
            impact.notes.append(
                f"副本数将从 {current} 增加到 {target_replicas}，会新建 {impact.pods_added} 个实例"
            )

    labels = _workload_labels(wl)

    # --- Pod 实际状态 ---
    try:
        selector = (wl.spec.selector.match_labels or {}) if wl.spec.selector else {}
        sel = ",".join(f"{k}={v}" for k, v in selector.items())
        pods = k8s.list_pods(namespace, label_selector=sel)
        impact.pods_restarted = len(pods)
        if len(pods) != impact.replicas:
            impact.notes.append(
                f"实际 Pod 数 {len(pods)} 与期望副本数 {impact.replicas} 不一致"
            )
    except K8sUnavailable as exc:
        impact.notes.append(f"无法列出 Pod: {exc}")

    # --- PDB ---
    try:
        pdbs = k8s.list_pdb(namespace)
        matched = [p for p in pdbs if _selector_matches(p.get("selector") or {}, labels)]
        if matched:
            p = matched[0]
            if p.get("min_available") is not None:
                impact.pdb = f"minAvailable={p['min_available']}"
            elif p.get("max_unavailable") is not None:
                impact.pdb = f"maxUnavailable={p['max_unavailable']}"
            else:
                impact.pdb = f"{p['name']}（规则未知）"
            allowed = p.get("disruptions_allowed")
            if allowed is not None:
                impact.pdb += f"，当前允许中断 {allowed} 个"
                if int(allowed) == 0:
                    impact.notes.append("PDB 当前不允许任何中断，驱逐类操作会失败或被阻塞")
        else:
            impact.pdb = "无 PDB 保护"
            if impact.single_point:
                impact.notes.append("单副本且无 PDB，重启期间服务可能中断")
    except Exception as exc:  # noqa: BLE001
        impact.pdb = f"未知（{exc}）"

    # --- 上游依赖：哪些 Service 选中了这个工作负载 ---
    try:
        services = k8s.core.list_namespaced_service(namespace).items
        deps = []
        for svc in services:
            svc_sel = dict(svc.spec.selector or {})
            if svc_sel and _selector_matches(svc_sel, labels):
                deps.append(f"Service/{svc.metadata.name}")
        impact.upstream_deps = deps
        if deps:
            impact.notes.append(f"有 {len(deps)} 个 Service 指向该负载，重启期间流量会重路由")
    except Exception as exc:  # noqa: BLE001
        impact.notes.append(f"无法查询 Service 依赖: {exc}")

    return impact


def analyse_node_impact(k8s: K8sClient, node: str) -> Impact:
    """分析对某个节点执行 cordon/drain 的影响面。"""
    impact = Impact()
    try:
        pods = k8s.pods_on_node(node)
    except Exception as exc:  # noqa: BLE001
        impact.notes.append(f"无法列出节点上的 Pod: {exc}")
        return impact

    impact.nodes_affected = 1
    impact.pods_restarted = len(pods)
    not_ready = [p.name for p in pods if not p.ready]
    if not_ready:
        impact.notes.append(f"节点上有 {len(not_ready)} 个未就绪 Pod")
    if len(pods) > 10:
        impact.notes.append(f"节点上承载 {len(pods)} 个 Pod，drain 影响面较大")
    return impact
