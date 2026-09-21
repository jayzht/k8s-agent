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
) -> Impact:
    """分析对某个工作负载执行变更的影响面。"""
    impact = Impact()
    try:
        wl = k8s.read_workload(namespace, kind, name)
    except Exception as exc:  # noqa: BLE001
        impact.notes.append(f"无法读取工作负载，影响面未知: {exc}")
        return impact

    impact.replicas = int(wl.spec.replicas or 0)
    impact.stateful = kind.lower() == "statefulset"
    impact.single_point = impact.replicas <= 1
    impact.has_pvc = _detect_pvc(wl, kind)

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


def summarise(impact: Impact) -> list[tuple[str, str]]:
    """把影响面转成确认卡片用的 (标签, 值) 列表。缺失值显式标注。"""
    def val(v: Any, unknown: str = "未知") -> str:
        if v is None or v == "":
            return unknown
        return str(v)

    return [
        ("影响副本", f"{impact.replicas} 个" if impact.replicas else "未知"),
        ("影响 Pod 数", f"{impact.pods_restarted} 个"),
        ("是否有状态", "是（StatefulSet）" if impact.stateful else "否（无状态）"),
        ("是否单点", "⚠️ 是（单副本）" if impact.single_point else "否"),
        ("持久卷挂载", "⚠️ 是" if impact.has_pvc else "否"),
        ("PDB 约束", val(impact.pdb, "未知")),
        ("上游 Service", "、".join(impact.upstream_deps) if impact.upstream_deps else "无"),
        ("影响节点数", str(impact.nodes_affected)),
    ]
