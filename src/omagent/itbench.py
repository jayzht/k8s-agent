"""ITBench-Lite 数据接入：用**真实 K8s 事故数据**评测诊断能力。

为什么需要它
------------
本项目的 50 条用例全是自己写的，规则引擎 100% 通过——高度自证。
ITBench-Lite（IBM Research）提供了**我没有参与构造的**真实事故数据：
35 个场景，每个含真实 K8s 对象快照、事件、OTel 日志/链路，以及人工标注的根因真值。

能力范围的重要澄清（读完数据才发现的）
--------------------------------------
ITBench 的根因分布是：ConfigMap ×12、Chaos 实验对象 ×11、Pod ×3、Deployment ×2，
其余为 Namespace/HPA/Schedule 等。

- **Chaos 类根因**（NetworkChaos/StressChaos/JVMChaos，位于 chaos-mesh 命名空间）
  是**故障注入器本身**，我的 Agent 永远不会、也不该把它当作根因。
- **ConfigMap / 功能开关类**（如 flagd）根因在配置，我的动作白名单里没有"改配置"。
- 真正落在本项目能力范围内的只有 **Pod / Deployment 类**，
  且机制要能对应到我的故障分类（如 invalid-node-selector → pending）。

所以：**ITBench 只覆盖本项目能力的一小部分，但它提供的是独立真值。**
本模块的意义不是"跑一个高分"，而是**拿到一个不是自己出的分数**。

另一个必须说清的局限
--------------------
本项目当前的能力是"**给我一个工作负载，我诊断它**"，而 ITBench 考的是
"**从全集群遥测里定位是哪个实体出问题**"——后者需要的能力我还没做
（意图层是从用户描述推断，不是从遥测推断）。
因此本模块的做法是：**用真值指定工作负载，只评"诊断与处置"这一半**，
并在报告里明确标注这不是"根因定位"的分数。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

csv.field_size_limit(10**9)

# ---------------------------------------------------------------------------
# 故障机制 → 本项目故障分类的映射
#
# ⚠️ 这张表是**人工核对**出来的，不是从数据里推出来的。
# 只有确实能在本项目分类体系里表达的机制才登记；其余一律标 unknown，
# 并且在统计时单独列出，**不计入准确率分母**——否则会拿一个
# "Agent 本来就不该负责的故障"去算它答错。
# ---------------------------------------------------------------------------
MECHANISM_TO_SIGNATURE: dict[str, str | None] = {
    "invalid-node-selector": "pending_unschedulable",
    "environment_variable_misconfiguration": None,   # 症状决定：可能 crashloop，也可能 not_ready
    "custom": None,                                   # 语义不明，需逐个看
    "custom(flagd)": None,                            # 功能开关，不在本项目动作空间
}

# 这些根因类型明确超出本项目范围（不参与评分）
OUT_OF_SCOPE_KINDS = {
    "ConfigMap", "Namespace", "HorizontalPodAutoscaler", "Schedule",
    "NetworkChaos", "StressChaos", "JVMChaos", "PodChaos", "IOChaos",
    "HTTPChaos", "DNSChaos", "TimeChaos", "KernelChaos",
}


@dataclass
class Scenario:
    """一个 ITBench 场景的解析结果。"""

    scn_id: str
    objects: dict[tuple, dict] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    ground_truth: dict[str, Any] = field(default_factory=dict)

    # --- 真值 -------------------------------------------------------------
    @property
    def fault_entities(self) -> list[dict]:
        return [(f.get("entity") or {}) for f in (self.ground_truth.get("fault") or [])]

    @property
    def root_cause_groups(self) -> list[dict]:
        return [g for g in (self.ground_truth.get("groups") or []) if g.get("root_cause")]

    @property
    def root_cause_kinds(self) -> set[str]:
        return {g.get("kind", "") for g in self.root_cause_groups}

    @property
    def mechanisms(self) -> list[str]:
        return [f.get("fault_mechanism", "") for f in (self.ground_truth.get("fault") or [])]

    @property
    def in_scope(self) -> bool:
        """这个场景是否落在本项目的能力范围内。"""
        kinds = self.root_cause_kinds or {
            (f.get("entity") or {}).get("kind", "") for f in (self.ground_truth.get("fault") or [])
        }
        if kinds & OUT_OF_SCOPE_KINDS:
            return False
        return bool(kinds & {"Pod", "Deployment", "StatefulSet"})

    def objects_of(self, kind: str) -> list[dict]:
        return [o for (k, _ns, _n), o in self.objects.items() if k == kind]

    def find(self, kind: str, namespace: str, name: str) -> dict | None:
        return self.objects.get((kind, namespace, name))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def load_objects(path: str | Path) -> dict[tuple, dict]:
    """把对象 TSV 解析成 {（kind, ns, name): 最后一次快照}。

    TSV 里同一个对象会出现多次（不同时间点的快照），只保留最后一份——
    那才是故障发生后的状态。
    """
    latest: dict[tuple, tuple[str, dict]] = {}
    if not Path(path).exists():
        # 缺文件不应让整批 35 个场景的评测崩掉，返回空即可
        return {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            try:
                payload = json.loads(row.get("Body") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            obj = payload.get("object", payload)
            if not isinstance(obj, dict) or not obj.get("kind"):
                continue
            md = obj.get("metadata") or {}
            key = (obj["kind"], md.get("namespace"), md.get("name"))
            ts = row.get("Timestamp", "")
            if key not in latest or ts >= latest[key][0]:
                latest[key] = (ts, obj)
    return {k: v[1] for k, v in latest.items()}


def load_events(path: str | Path) -> list[dict]:
    """从 K8s 事件 TSV 里提取事件（Body 内嵌 event JSON）。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            try:
                payload = json.loads(row.get("Body") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            ev = payload.get("object", payload)
            if not isinstance(ev, dict):
                continue
            inv = ev.get("involvedObject") or ev.get("regarding") or {}
            if not inv.get("kind"):
                continue
            key = (inv.get("kind"), inv.get("name"), ev.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "type": ev.get("type", "Normal"),
                "reason": ev.get("reason", ""),
                "object": f"{inv.get('kind')}/{inv.get('name')}",
                "message": (ev.get("message") or ev.get("note") or "")[:300],
                "namespace": inv.get("namespace", ""),
            })
    return out


def load_scenario(base: str | Path, scn_id: str) -> Scenario:
    base = Path(base)
    gt_path = base / "gt" / f"{scn_id}.yaml"
    return Scenario(
        scn_id=scn_id,
        objects=load_objects(base / "data" / f"{scn_id}_k8s_objects_raw.tsv"),
        events=load_events(base / "data" / f"{scn_id}_k8s_events_raw.tsv"),
        ground_truth=(yaml.safe_load(gt_path.read_text(encoding="utf-8"))
                      if gt_path.exists() else {}) or {},
    )


# ---------------------------------------------------------------------------
# 真值工作负载 → 本项目 fixture
# ---------------------------------------------------------------------------


def _selector_of(deploy: dict) -> dict[str, str]:
    return dict(((deploy.get("spec") or {}).get("selector") or {}).get("matchLabels") or {})


def _pod_view(pod: dict) -> dict[str, Any]:
    """真实 Pod 对象 → 本项目 fixture 的 pod 视图。"""
    st = pod.get("status") or {}
    statuses = st.get("containerStatuses") or []
    last_exit, term_reason = None, ""
    for cs in statuses:
        term = ((cs.get("lastState") or {}).get("terminated") or {})
        if term:
            last_exit = term.get("exitCode")
            term_reason = term.get("reason") or ""
    waiting_reason, message = "", ""
    for cs in statuses:
        w = ((cs.get("state") or {}).get("waiting") or {})
        if w:
            waiting_reason = w.get("reason") or ""
            message = w.get("message") or ""
    # Pending 的原因通常在 status.conditions / status.reason 上
    if not waiting_reason:
        waiting_reason = st.get("reason") or ""
        message = message or st.get("message") or ""
    limit = ""
    for c in (pod.get("spec") or {}).get("containers") or []:
        lim = ((c.get("resources") or {}).get("limits") or {})
        if lim.get("memory"):
            limit = lim["memory"]

    return {
        "name": (pod.get("metadata") or {}).get("name", ""),
        "phase": st.get("phase") or "",
        "ready": all(cs.get("ready") for cs in statuses) if statuses else False,
        "restarts": sum(cs.get("restartCount") or 0 for cs in statuses),
        "last_exit_code": last_exit,
        "termination_reason": term_reason,
        "reason": waiting_reason,
        "message": message[:200],
        "memory_limit": limit,
        "node": (pod.get("spec") or {}).get("nodeName") or "",
        "labels": dict((pod.get("metadata") or {}).get("labels") or {}),
    }


def build_fixture(scn: Scenario) -> dict[str, Any] | None:
    """按真值根因，从真实快照里裁剪出本项目格式的 fixture。

    做法：找到真值指向的 Deployment（Pod 类根因则回溯其 owner），
    再用它的 selector 把相关 Pod / Service / Endpoints 拉进来。
    """
    groups = scn.root_cause_groups
    if not groups:
        return None
    g = groups[0]
    ns = g.get("namespace") or "otel-demo"
    kind = g.get("kind")

    deploy: dict | None = None
    if kind == "Deployment":
        name = _name_from_filter(g, scn, "Deployment", ns)
        deploy = scn.find("Deployment", ns, name) if name else None
    elif kind == "Pod":
        # Pod → 顺 ownerReferences 找到 ReplicaSet → Deployment
        pname = _name_from_filter(g, scn, "Pod", ns)
        pod = scn.find("Pod", ns, pname) if pname else None
        if pod:
            for ref in (pod.get("metadata") or {}).get("ownerReferences") or []:
                if ref.get("kind") == "ReplicaSet":
                    rs = scn.find("ReplicaSet", ns, ref.get("name"))
                    for r2 in ((rs or {}).get("metadata") or {}).get("ownerReferences") or []:
                        if r2.get("kind") == "Deployment":
                            deploy = scn.find("Deployment", ns, r2.get("name"))
    if not deploy:
        return None

    labels = _selector_of(deploy)
    dep_name = (deploy.get("metadata") or {}).get("name", "")
    pods = [
        p for p in scn.objects_of("Pod")
        if (p.get("metadata") or {}).get("namespace") == ns
        and _matches(dict((p.get("metadata") or {}).get("labels") or {}), labels)
    ]
    services = [
        s for s in scn.objects_of("Service")
        if (s.get("metadata") or {}).get("namespace") == ns
        and _matches(dict((s.get("spec") or {}).get("selector") or {}), labels)
    ]
    endpoints = {}
    for s in services:
        ep = scn.find("Endpoints", ns, (s.get("metadata") or {}).get("name", ""))
        ready = 0
        if ep:
            for sub in ep.get("subsets") or []:
                ready += len(sub.get("addresses") or [])
        endpoints[(s.get("metadata") or {}).get("name", "")] = ready

    relevant_events = [
        e for e in scn.events
        if e.get("namespace") == ns
        and (dep_name in (e.get("object") or "") or any(
            p["name"] in (e.get("object") or "") for p in map(_pod_view, pods)
        ))
    ][:25]

    return {
        "namespace": ns,
        "workload": {
            "kind": "Deployment",
            "name": dep_name,
            "replicas": (deploy.get("spec") or {}).get("replicas") or 0,
            "labels": labels,
        },
        "pods": [_pod_view(p) for p in pods],
        "services": [ (s.get("metadata") or {}).get("name", "") for s in services ],
        "endpoints": endpoints,
        "events": relevant_events,
        "nodes": {
            (n.get("metadata") or {}).get("name", ""): bool((n.get("spec") or {}).get("unschedulable"))
            for n in scn.objects_of("Node")
        },
        # 配置层诊断要用：工作负载的 env（答案常常就在这儿）
        "env": {
            e["name"]: e["value"]
            for e in _env_of(deploy)
            if e.get("name") and e.get("value")
        },
        # 应用要去连的那些 Service 的真实端口，用于与配置交叉比对
        "service_ports": _all_service_ports(scn, ns),
        "deployment_spec": {
            "nodeSelector": ((deploy.get("spec") or {}).get("template", {}).get("spec", {}) or {}).get("nodeSelector") or {},
            "env": [
                {"name": e.get("name"), "value": (e.get("value") or "")[:80]}
                for c in ((deploy.get("spec") or {}).get("template", {}).get("spec", {}) or {}).get("containers") or []
                for e in (c.get("env") or [])
            ][:12],
        },
    }


def _env_of(deploy: dict) -> list[dict[str, str]]:
    """取 Deployment pod template 里所有容器的 env（只取字面量，跳过 valueFrom）。"""
    spec = (deploy.get("spec") or {}).get("template", {}).get("spec", {}) or {}
    out = []
    for c in spec.get("containers") or []:
        for e in c.get("env") or []:
            out.append({"name": e.get("name", ""), "value": e.get("value") or ""})
    return out


def _all_service_ports(scn: Scenario, ns: str) -> dict[str, list[int]]:
    """命名空间内所有 Service 的端口——用于判断 env 里写的端口是否真实存在。"""
    out: dict[str, list[int]] = {}
    for svc in scn.objects_of("Service"):
        if (svc.get("metadata") or {}).get("namespace") != ns:
            continue
        ports = [p.get("port") for p in ((svc.get("spec") or {}).get("ports") or []) if p.get("port")]
        if ports:
            out[(svc.get("metadata") or {}).get("name", "")] = ports
    return out


def _matches(labels: dict[str, str], selector: dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _name_from_filter(group: dict, scn: Scenario, kind: str, ns: str) -> str | None:
    """真值的 filter 是正则片段，用它去实际对象里挑出名字。"""
    import re

    filt = (group.get("filter") or [None])[0]
    if not filt:
        return None
    pat = re.compile(filt)
    for (k, on, n) in scn.objects:
        if k == kind and on == ns and n and pat.search(n):
            return n
    return None


# ---------------------------------------------------------------------------
# 人工核对的期望值
#
# ⚠️ 这张表是**逐个看过真实快照后手写的**，不是从数据自动推出来的。
# 自动从真值推期望会导致"用我的分类器生成期望、再用它考我的分类器"的循环论证。
# ---------------------------------------------------------------------------
REVIEWED_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "Scenario-33": {
        "expected_signature": "pending_unschedulable",
        "cause_keywords": ["节点", "nodeselector", "node selector", "选择器", "调度"],
        "note": "invalid-node-selector：新 Pod 因不可满足的 nodeSelector 卡在 Pending，"
                "旧 Pod 仍在服务（滚动更新卡住）",
    },
    "Scenario-16": {
        "expected_signature": "config_misconfiguration",
        "cause_keywords": ["环境变量", "quote_addr", "端口", "port", "配置"],
        "note": "Pod 全部就绪，故障是 env 里的 QUOTE_ADDR 指向了错误端口；"
                "配置层实现后应能识别（原先被判为「设计上看不到」）",
    },
    "Scenario-24": {
        "expected_signature": "config_misconfiguration",
        "cause_keywords": ["环境变量", "kafka_addr", "配置", "端口"],
        "note": "env var 配错导致新容器未就绪；配置层实现后应能识别并建议回滚",
    },
}


@dataclass
class ScenarioResult:
    scn_id: str
    in_scope: bool = False
    built: bool = False
    workload: str = ""
    signature_expected: str = ""
    signature_actual: str = ""
    signature_match: bool = False
    cause_mentioned: bool = False
    undetectable: bool = False
    mutating_proposals: list[str] = field(default_factory=list)
    conclusion: str = ""
    note: str = ""
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error:
            return "error"
        if not self.built:
            return "skip"
        if self.undetectable:
            # 设计上看不到的故障：只记录，不算答错
            return "undetectable"
        if self.signature_match and self.cause_mentioned:
            return "hit"
        if self.signature_match:
            return "partial"
        return "miss"


def run_scenario(base: str | Path, scn_id: str, *, audit_dir: Path | None = None) -> ScenarioResult:
    """构建 fixture 并跑规则引擎，按独立口径评分。"""
    from .evals import EvalCase, run_case

    scn = load_scenario(base, scn_id)
    res = ScenarioResult(scn_id=scn_id, in_scope=scn.in_scope)
    if not scn.in_scope:
        res.note = f"根因类型 {scn.root_cause_kinds} 超出本项目能力范围"
        return res

    fx = build_fixture(scn)
    if not fx:
        res.built = False
        res.error = "无法从真实快照里定位真值工作负载"
        return res

    exp = REVIEWED_EXPECTATIONS.get(scn_id, {})
    res.built = True
    res.workload = f"{fx['workload']['kind']}/{fx['workload']['name']}"
    res.note = exp.get("note", "")
    res.undetectable = bool(exp.get("undetectable"))

    case = EvalCase.from_dict({
        "id": f"itbench-{scn_id}",
        "title": f"ITBench {scn_id}",
        "namespace": fx["namespace"],
        "fixture": {
            "workload": fx["workload"],
            "pods": fx["pods"],
            "services": fx["services"],
            "endpoints": fx["endpoints"],
            "events": fx["events"],
            "nodes": fx["nodes"],
            "env": fx.get("env") or {},
            "service_ports": fx.get("service_ports") or {},
        },
        "expect": {
            "signature": exp.get("expected_signature", ""),
            "acceptable_actions": [],
            "forbidden_actions": ["delete_workload", "delete_namespace"],
        },
    })
    r = run_case(case, audit_dir=audit_dir)
    res.signature_expected = exp.get("expected_signature", "")
    res.signature_actual = r.signature_actual
    res.signature_match = res.signature_actual == res.signature_expected
    res.mutating_proposals = r.mutating_proposals
    res.conclusion = r.conclusion

    plain = (r.conclusion or "").lower()
    res.cause_mentioned = any(k.lower() in plain for k in exp.get("cause_keywords", []))
    return res


def summarise_results(results: list[ScenarioResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r.verdict] = out.get(r.verdict, 0) + 1
    return out
