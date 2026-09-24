"""监控台数据：把集群状态整理成"一眼能看出哪儿不对"的列表。

这是给运维人员看的**入口**——他先在这里看见红点，然后才去右边的对话框问
"这怎么回事"。所以这里的信息组织原则是：**异常优先**。
健康的负载折叠成一行，有问题的负载把原因写在脸上。
"""

from __future__ import annotations

from typing import Any

from .k8s import K8sClient, K8sUnavailable


def _selector_matches(selector: dict[str, str] | None, labels: dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _workload_row(k8s: K8sClient, namespace: str, kind: str, obj: Any, pods: list[Any]) -> dict[str, Any]:
    """把一个 Deployment/StatefulSet/DaemonSet 整理成一行监控台条目。

    三种类型的"期望副本数"来源不一样，这里必须分开取：
    Deployment/StatefulSet 看 ``spec.replicas``，DaemonSet **根本没有这个字段**
    （它由节点数决定），期望值在 ``status.desired_number_scheduled``。
    早期版本一刀切读 ``spec.replicas``，DaemonSet 一进来就抛 AttributeError。
    """
    selector = getattr(getattr(obj.spec, "selector", None), "match_labels", None) or {}
    mine = [p for p in pods if _selector_matches(selector, p.labels)]

    if kind == "daemonset":
        desired = obj.status.desired_number_scheduled or 0
        ready = obj.status.number_ready or 0
        available = ready
    else:
        desired = getattr(obj.spec, "replicas", None) or 0
        ready = obj.status.ready_replicas or 0
        available = obj.status.available_replicas or 0

    problems: list[str] = []
    # desired==0 是一个值得标出来的状态：一个本该在跑的工作负载被缩到 0，
    # 看副本数 0/0 很容易被当成"没事"。这是"依赖被关掉了"这类故障的唯一可见信号。
    if desired == 0 and kind != "daemonset":
        problems.append("已缩容到 0 副本（服务当前没有实例）")
    elif desired and ready < desired:
        problems.append(f"就绪 {ready}/{desired}")
    for p in mine:
        if not p.healthy:
            reason = p.problem or f"{p.phase}"
            problems.append(f"{p.name}: {reason}")
        elif p.restarts >= 3:
            problems.append(f"{p.name}: 已重启 {p.restarts} 次")

    # 去重但保持顺序
    seen: set[str] = set()
    problems = [x for x in problems if not (x in seen or seen.add(x))]

    images: list[str] = []
    container_ports: set[int] = set()
    configmaps: list[str] = []
    node_pool = ""
    tmpl = getattr(obj.spec, "template", None)
    if tmpl is not None:
        for c in (tmpl.spec.containers or []):
            images.append(c.image)
            for port in (getattr(c, "ports", None) or []):
                if getattr(port, "container_port", None):
                    container_ports.add(int(port.container_port))
            for src in (getattr(c, "env_from", None) or []):
                ref = getattr(src, "config_map_ref", None)
                if ref is not None and getattr(ref, "name", None):
                    configmaps.append(ref.name)
        node_pool = dict(getattr(tmpl.spec, "node_selector", None) or {}).get("node-pool", "")

    return {
        "namespace": namespace,
        "kind": kind,
        "name": obj.metadata.name,
        "desired": desired,
        "ready": ready,
        "available": available,
        "healthy": not problems,
        "problems": problems[:8],
        "problem_count": len(problems),
        "images": images,
        "container_ports": sorted(container_ports),
        # 下面这几个是「能不能拿它演某个故障」的判据，给前端的故障菜单用。
        # 前端据此把不支持的场景按钮置灰，而不是让人点了之后吃一个报错。
        "configmaps": configmaps,
        "has_configmap": bool(configmaps),
        "node_pool": node_pool,
        "has_node_selector": bool(node_pool),
        "labels": dict(obj.metadata.labels or {}),
        "pods": [
            {
                "name": p.name,
                "phase": p.phase,
                "ready": p.ready,
                "restarts": p.restarts,
                "node": p.node,
                "reason": p.problem or (p.reason or ""),
                "last_exit_code": p.last_exit_code,
                "termination_reason": p.termination_reason,
            }
            for p in sorted(mine, key=lambda x: x.name)
        ],
        "selector": selector,
    }


def service_and_hpa_health(
    k8s: K8sClient, namespace: str, rows: list[dict[str, Any]]
) -> tuple[dict[str, list[str]], list[dict[str, Any]], dict[str, dict[str, bool]]]:
    """Service / Endpoints / HPA 三类"资源健康之外"的异常信号。

    同时返回每个工作负载的**能力标记**（有没有 Service / HPA），
    前端据此决定"制造故障"菜单里哪些场景对这个目标是可用的。
    """
    per_workload: dict[str, list[str]] = {}
    standalone: list[dict[str, Any]] = []
    caps: dict[str, dict[str, bool]] = {r["name"]: {"has_service": False, "has_hpa": False}
                                        for r in rows}

    try:
        services = k8s.list_services(namespace)
        endpoints = {e["name"]: e for e in k8s.list_endpoints(namespace)}
    except Exception:  # noqa: BLE001
        services, endpoints = [], {}

    for svc in services:
        name = svc["name"]
        if name in ("kubernetes",):
            continue
        selector = svc.get("selector") or {}
        ep = endpoints.get(name)
        ready = int((ep or {}).get("ready") or 0)
        not_ready = int((ep or {}).get("not_ready") or 0)

        # 这个 Service 指向哪个工作负载（按 selector 匹配）
        target = None
        for r in rows:
            if selector and r["selector"] and all(
                r["selector"].get(k) == v for k, v in selector.items()
            ):
                target = r["name"]
                break
        # 同名 Service 也算——"能不能拿它演选择器写错"看的是有没有 Service 可改
        if name in caps:
            caps[name]["has_service"] = True
        if target and target in caps:
            caps[target]["has_service"] = True

        issues: list[str] = []
        if not selector:
            pass  # 没有 selector 的 Service（如 ExternalName）不判断
        elif ready == 0 and not_ready == 0:
            issues.append(f"Service/{name}: 没有任何后端（selector 可能写错了）")
        elif ready == 0 and not_ready > 0:
            issues.append(f"Service/{name}: 后端全部未就绪（{not_ready} 个）")
        elif not_ready > 0:
            issues.append(f"Service/{name}: 有 {not_ready} 个后端未就绪")

        # targetPort 与容器端口交叉比对：端口对不上时 Endpoints 看起来是正常的，
        # 但连接会被拒绝——只看 Endpoints 有地址是不够的。
        if target:
            row = next(r for r in rows if r["name"] == target)
            cports = set(row.get("container_ports") or [])
            if cports:
                for p in svc.get("ports") or []:
                    tp = str(p.get("target_port") or "")
                    if tp.isdigit() and int(tp) not in cports:
                        issues.append(
                            f"Service/{name}: targetPort {tp} 不在容器的监听端口 "
                            f"{sorted(cports)} 里（流量会被拒绝）"
                        )

        for msg in issues:
            if target:
                per_workload.setdefault(target, []).append(msg)
            else:
                standalone.append({"name": name, "problems": [msg]})

    # HPA：一个处于失败状态的 HPA 意味着自动扩缩容已经失效。
    #
    # 只认 `AbleToScale=False`，**不看** `ScalingActive`。
    #
    # 以前这个沙箱没装 metrics-server，`ScalingActive` 会永远是 False 且理由是
    # FailedGetResourceMetric —— 那是环境缺监控组件，不是这个工作负载的问题。
    # 拿它当判据的话，每个挂了 HPA 的负载都会常年标红，信号就废掉。
    # 现在 metrics-server 装上了，基线是 ScalingActive=True，
    # 但这个判据仍然只认 AbleToScale——它才真正表示"扩缩容目标坏了"。
    try:
        hpas = k8s.autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace).items
    except Exception:  # noqa: BLE001
        hpas = []

    names = {r["name"] for r in rows}
    for h in hpas:
        ref = getattr(h.spec.scale_target_ref, "name", "") or ""
        # 目标解析不到时，退回按 HPA 自己的名字找归属——按惯例它和工作负载同名，
        # 而操作员盯着的是工作负载那一行，不是 HPA 那一行。
        # 故障场景恰好就是"目标被改成不存在的名字"，此时 ref 必然匹配不上。
        owner = ref if ref in names else (h.metadata.name if h.metadata.name in names else "")
        if owner:
            caps[owner]["has_hpa"] = True

        bad = [
            f"{c.reason} — {(c.message or '')[:120]}"
            for c in ((h.status.conditions or []) if h.status else [])
            if c.type == "AbleToScale" and c.status == "False"
        ]
        if bad:
            msg = f"HPA/{h.metadata.name} 已失效（扩缩容不工作）：" + "；".join(bad)
            if owner:
                per_workload.setdefault(owner, []).append(msg)
            else:
                standalone.append({"name": h.metadata.name, "problems": [msg]})

    return per_workload, standalone, caps


def overview(k8s: K8sClient, namespace: str, cases: Any = None,
             nodes_cache: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """一个命名空间的监控台视图。"""
    try:
        pods = k8s.list_pods(namespace)
    except K8sUnavailable as exc:
        return {"ok": False, "error": str(exc), "workloads": [], "pods": [], "nodes": []}

    rows: list[dict[str, Any]] = []
    try:
        for dep in k8s.apps.list_namespaced_deployment(namespace).items:
            rows.append(_workload_row(k8s, namespace, "deployment", dep, pods))
        for sts in k8s.apps.list_namespaced_stateful_set(namespace).items:
            rows.append(_workload_row(k8s, namespace, "statefulset", sts, pods))
        for ds in k8s.apps.list_namespaced_daemon_set(namespace).items:
            rows.append(_workload_row(k8s, namespace, "daemonset", ds, pods))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读取工作负载失败：{exc}", "workloads": [], "pods": [],
                "nodes": [], "namespace": namespace}

    # 「Pod 健康」之外的信号：Service 有没有后端、HPA 有没有失效。
    # 这两类问题不会体现在 Pod 状态上，但恰恰是最常见的"重启也没用"的故障。
    try:
        extra_by_workload, standalone, caps = service_and_hpa_health(k8s, namespace, rows)
    except Exception:  # noqa: BLE001
        extra_by_workload, standalone, caps = {}, [], {}
    for r in rows:
        caps_row = caps.get(r["name"], {})
        r["has_service"] = bool(caps_row.get("has_service"))
        r["has_hpa"] = bool(caps_row.get("has_hpa"))
        extra = extra_by_workload.get(r["name"])
        if extra:
            r["problems"] = (r["problems"] + extra)[:8]
            r["problem_count"] = len(r["problems"])
            r["healthy"] = False

    # 语义记忆：这个症状以前处理过吗。
    #
    # 放在监控台而不是只给 Agent 用，是因为**人也需要知道**：
    # "上次这个症状是这么修的"对值班的人同样有价值，
    # 而且它让人能判断 Agent 的建议是不是有先例可循。
    if cases is not None:
        for r in rows:
            if r["healthy"]:
                r["known_cases"] = 0
                r["case_hint"] = ""
                continue
            try:
                from .cases import describe, symptom_tokens

                toks = symptom_tokens(k8s, namespace, r["kind"], r["name"],
                                      pods=[p for p in pods
                                            if _selector_matches(r["selector"], p.labels)])
                hits = cases.search(toks, limit=3)
                r["known_cases"] = len(hits)
                if hits:
                    score, c = hits[0]
                    r["case_hint"] = (
                        f"{c.workload} 出过同样的症状，当时用 {c.tool} 修好了、"
                        f"事后回查确认症状消失（匹配 {int(score * 100)}%，{c.operator} 批准）"
                    )
                else:
                    r["case_hint"] = ""
                r["symptoms"] = describe(toks)
            except Exception:  # noqa: BLE001
                r["known_cases"] = 0
                r["case_hint"] = ""

    # 异常优先，其次按名字
    rows.sort(key=lambda r: (r["healthy"], r["name"]))

    # 没被任何工作负载选中的 Pod（裸 Pod、Job 等）——也值得显示，它们出问题时
    # 恰恰最容易被忽略
    claimed = {p["name"] for r in rows for p in r["pods"]}
    orphans = [p for p in pods if p.name not in claimed]

    events: list[dict[str, Any]] = []
    try:
        # 同一个对象反复报同一个原因是常态（BackOff/Unhealthy 会每秒刷一条）。
        # 按「对象 + 原因」去重，只留最新一条——把 40 条噪音压成 3 条可读的信号。
        seen_ev: set[tuple[str, str]] = set()
        for e in k8s.list_events(namespace):
            if e.get("type") != "Warning":
                continue
            key = (str(e.get("object") or ""), str(e.get("reason") or ""))
            if key in seen_ev:
                continue
            seen_ev.add(key)
            events.append(e)
    except Exception:  # noqa: BLE001
        pass

    # 节点列表是全局的，wall() 一次刷新要读三个命名空间——
    # 允许调用方传进来复用，省掉 N-1 次 list_node（实测从 4 次降到 1 次）
    nodes: list[dict[str, Any]] = list(nodes_cache) if nodes_cache is not None else []
    if nodes_cache is None:
        try:
            nodes = k8s.list_nodes()
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "namespace": namespace,
        "workloads": rows,
        "standalone": standalone,
        "orphan_pods": [
            {"name": p.name, "phase": p.phase, "ready": p.ready, "restarts": p.restarts,
             "reason": p.problem or p.reason, "node": p.node}
            for p in orphans
        ],
        "nodes": nodes,
        "warning_events": events[:20],
        "summary": {
            "workloads": len(rows),
            "unhealthy": sum(1 for r in rows if not r["healthy"]),
            "standalone": len(standalone),
            "with_cases": sum(1 for r in rows if r.get("known_cases")),
            "nodes": len(nodes),
            "nodes_unschedulable": sum(1 for n in nodes if n.get("unschedulable")),
        },
    }


def namespaces(k8s: K8sClient) -> list[str]:
    try:
        return sorted(k8s.namespaces())
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 大屏视图
#
# 和监控台的区别不是"内容更多"，而是**用途不同**：
#   · 监控台：坐在工位上排查，一个命名空间，可以点、可以问
#   · 大屏：挂在墙上，跨所有命名空间，没人操作，5 秒自刷
# 所以大屏要的是"扫一眼就知道有没有事"，而不是"能下钻"。
# ---------------------------------------------------------------------------


def wall(
    k8s: K8sClient,
    namespaces: list[str],
    *,
    cases: Any = None,
    activity: list[dict[str, Any]] | None = None,
    activity_limit: int = 8,
    case_limit: int = 5,
) -> dict[str, Any]:
    """把整个集群压成一块屏能看下的东西。"""
    groups: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    all_workloads = 0
    all_unhealthy = 0

    shared_nodes: list[dict[str, Any]] = []
    try:
        shared_nodes = k8s.list_nodes()
    except Exception:  # noqa: BLE001
        pass

    for ns in namespaces:
        data = overview(k8s, ns, cases=cases, nodes_cache=shared_nodes)
        if not data.get("ok"):
            groups.append({"namespace": ns, "error": data.get("error"), "workloads": [],
                           "unhealthy": 0})
            continue
        rows = data["workloads"]
        groups.append({
            "namespace": ns,
            "unhealthy": data["summary"]["unhealthy"] + data["summary"].get("standalone", 0),
            "workloads": [
                {
                    "name": w["name"], "kind": w["kind"],
                    "ready": w["ready"], "desired": w["desired"],
                    "healthy": w["healthy"],
                    "problem_count": w["problem_count"],
                    "problems": w["problems"][:2],
                    "known_cases": w.get("known_cases", 0),
                    "restarts": sum(p["restarts"] for p in w.get("pods", [])),
                }
                for w in rows
            ],
            "standalone": data.get("standalone", []),
            "events": [e for e in data.get("warning_events", [])][:6],
        })
        all_workloads += data["summary"]["workloads"]
        all_unhealthy += data["summary"]["unhealthy"]
        for w in rows:
            if not w["healthy"]:
                alerts.append({
                    "namespace": ns, "name": w["name"], "kind": w["kind"],
                    "ready": w["ready"], "desired": w["desired"],
                    "problems": w["problems"][:3],
                    "known_cases": w.get("known_cases", 0),
                    "case_hint": w.get("case_hint", ""),
                })
        for item in data.get("standalone", []):
            alerts.append({
                "namespace": ns, "name": item["name"], "kind": "service",
                "ready": 0, "desired": 0, "problems": item["problems"][:2],
                "known_cases": 0, "case_hint": "",
            })

    # 节点：可调度状态 + 实时利用率 + 上面压了多少 Pod
    nodes: list[dict[str, Any]] = []
    raw_nodes = shared_nodes
    usage = k8s.node_usage() if raw_nodes else {}
    try:
        pod_count = k8s.pods_per_node()   # 全部命名空间，一次调用
    except Exception:  # noqa: BLE001
        pod_count = {}

    cpu_sum = mem_sum = cpu_alloc = mem_alloc = 0.0
    for n in raw_nodes:
        u = usage.get(n["name"], {})
        cpu_a = float(n.get("cpu_allocatable") or 0)
        mem_a = float(n.get("mem_allocatable") or 0)
        cpu_u = float(u.get("cpu_cores") or 0)
        mem_u = float(u.get("mem_bytes") or 0)
        cpu_sum += cpu_u
        mem_sum += mem_u
        cpu_alloc += cpu_a
        mem_alloc += mem_a
        nodes.append({
            "name": n["name"],
            "ready": n.get("ready", True),
            "unschedulable": n.get("unschedulable", False),
            "zone": (n.get("labels") or {}).get("topology.kubernetes.io/zone", ""),
            "pool": (n.get("labels") or {}).get("node-pool", ""),
            "pods": pod_count.get(n["name"], 0),
            "cpu_cores": round(cpu_u, 2),
            "mem_bytes": mem_u,
            "cpu_alloc": round(cpu_a, 1),
            "cpu_pct": round(cpu_u / cpu_a * 100) if cpu_a else None,
            "mem_pct": round(mem_u / mem_a * 100) if mem_a else None,
            "pressured": n.get("pressured") or [],
            "has_metrics": bool(u),
        })
    nodes.sort(key=lambda x: x["name"])

    # 最近处置：从审计里挑人真正做过的决定
    acts: list[dict[str, Any]] = []
    for r in (activity or [])[::-1]:
        if len(acts) >= activity_limit:
            break
        ev = r.get("event", "")
        p = r.get("payload") or {}
        if ev == "execution":
            acts.append({"ts": r.get("ts", ""), "kind": "exec",
                         "text": f"{p.get('status', '')} · {p.get('proposal_id', '')[:14]}"})
        elif ev == "decision":
            acts.append({"ts": r.get("ts", ""), "kind": "decision",
                         "text": f"{'批准' if p.get('approved') else '拒绝'} "
                                 f"（{p.get('operator', '')}）"})
        elif ev == "gate_violation":
            acts.append({"ts": r.get("ts", ""), "kind": "violation",
                         "text": f"门禁拦截：{p.get('reason', '')} · {p.get('tool', '')}"})
        elif ev == "case_recorded":
            acts.append({"ts": r.get("ts", ""), "kind": "case",
                         "text": f"沉淀案例：{p.get('workload', '')} · {p.get('tool', '')}"})
        elif ev == "sandbox_fault":
            acts.append({"ts": r.get("ts", ""), "kind": "fault",
                         "text": f"演示注入：{p.get('scenario', '')} → {p.get('target', '')}"})

    recent_cases: list[dict[str, Any]] = []
    if cases is not None:
        try:
            from .cases import describe

            for c in cases.all()[-case_limit:][::-1]:
                from .cases import OUTCOME_EFFECTIVE

                recent_cases.append({
                    "workload": c.workload, "tool": c.tool,
                    "operator": c.operator, "ts": c.ts,
                    "symptoms": describe(c.signature),
                    "verified": c.outcome == OUTCOME_EFFECTIVE,
                    "outcome": c.outcome,
                })
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "nodes": nodes,
        "groups": groups,
        "alerts": alerts,
        "activity": acts,
        "cases": recent_cases,
        "summary": {
            "namespaces": len(namespaces),
            "workloads": all_workloads,
            "unhealthy": all_unhealthy,
            "alert_total": len(alerts),
            "nodes": len(raw_nodes),
            "nodes_ready": sum(1 for n in nodes if n["ready"]),
            "nodes_unschedulable": sum(1 for n in nodes if n["unschedulable"]),
            "pods": sum(pod_count.values()),
            # **绝对量也要给**：演示集群在 32 核/128G 的节点上，
            # 按占比算永远是 0%，一块永远显示 0% 的大屏等于没显示。
            "cpu_cores": round(cpu_sum, 2),
            "cpu_alloc": round(cpu_alloc, 1),
            "mem_bytes": mem_sum,
            "mem_alloc": mem_alloc,
            "cpu_pct": round(cpu_sum / cpu_alloc * 100) if cpu_alloc else None,
            "mem_pct": round(mem_sum / mem_alloc * 100) if mem_alloc else None,
            "has_metrics": bool(usage),
        },
    }
