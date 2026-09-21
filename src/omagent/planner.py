"""诊断规划器。

设计要点：**规划器是可插拔的，而且不是安全边界。**
- ``RuleBasedPlanner``：确定性规则引擎，不依赖任何外部服务。作为默认实现，
  保证在无 LLM、无网络、模型服务故障时 Agent 依然可用（PRD 第 10 节问题 9 的降级路径）。
- ``LLMPlanner``：OpenAI 兼容接口实现，用于增强开放式问题的诊断能力。

两者产出同一套 ``Diagnosis`` / ``Candidate`` 契约，因此**下游的安全内核
（影响面、dry-run、熔断、审批门禁）完全不需要知道规划器是谁**。这正是
"安全感由 schema 强制，而不是靠模型自觉"（PRD 5.2）在架构上的体现：
即使 LLM 产生幻觉，它也只能提出一个 Candidate，不能执行任何东西。
"""

from __future__ import annotations

import json
import re
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .k8s import K8sClient, PodInfo, K8sUnavailable
from .models import Evidence


@dataclass
class Candidate:
    """一个候选处置动作。注意：这只是**建议**，不是可执行方案。"""

    tool: str
    params: dict[str, Any]
    rationale: str
    note: str = ""  # 例如"只缓解不根治"


@dataclass
class Diagnosis:
    namespace: str
    workload: str
    kind: str
    conclusion: str
    confidence: str = "medium"  # high | medium | low
    evidence: list[Evidence] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    matched_signature: str = ""


class Planner(Protocol):
    name: str

    def diagnose(
        self,
        namespace: str,
        workload: str,
        kind: str,
        extra_context: str | None = None,
        final_note: str | None = None,
    ) -> Diagnosis: ...


# ---------------------------------------------------------------------------
# 故障特征库
#
# 每条特征 = 一个可复用的「症状 → 根因 → 处置」条目。
# 这就是 PRD 说的知识沉淀：规则库是种子，运行期从真实故障中继续抽取。
# ---------------------------------------------------------------------------


def _looks_oom(pods: list[PodInfo]) -> bool:
    return any(p.last_exit_code == 137 for p in pods)


def _has_reason(pods: list[PodInfo], *needles: str) -> bool:
    blob = " ".join(f"{p.reason} {p.message}" for p in pods).lower()
    return any(n.lower() in blob for n in needles)


def _quantity_le(a: str, b: str) -> bool:
    """粗略比较两个 K8s quantity（如 '512Mi' <= '1Gi'）。解析失败时保守返回 True。"""
    units = {"": 1, "m": 0.001, "k": 1e3, "M": 1e6, "G": 1e9, "Ki": 1024,
             "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}

    def parse(q: str) -> float | None:
        q = str(q).strip()
        for suffix in sorted(units, key=len, reverse=True):
            if suffix and q.endswith(suffix):
                try:
                    return float(q[: -len(suffix)]) * units[suffix]
                except ValueError:
                    return None
        try:
            return float(q)
        except ValueError:
            return None

    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        return True
    return pa <= pb


def _pod_reason(p: PodInfo, *needles: str) -> bool:
    blob = f"{p.reason} {p.message}".lower()
    return any(n.lower() in blob for n in needles)


# 故障类别的展示顺序（越靠前越"根因优先"）
CATEGORY_ORDER = (
    "node_failure",
    "oom_killed",
    "probe_kill",
    "config_misconfiguration",
    "dependency_unavailable",
    "network_blocked",
    "service_target_port_mismatch",
    "node_pressure",
    "quota_exhausted",
    "ingress_backend_invalid",
    "limitrange_violation",
    "image_pull",
    "pending_unschedulable",
    "crashloop",
    "not_ready",
    "service_no_endpoints",
)

CATEGORY_LABEL = {
    "node_failure": "节点故障",
    "oom_killed": "内存超限（OOMKilled）",
    "probe_kill": "探针误杀（137 来自探针 SIGKILL，不是 OOM）",
    "config_misconfiguration": "配置指向错误地址（env 与服务实际端口不一致）",
    "dependency_unavailable": "依赖方自身不可用（不是本工作负载的问题）",
    "network_blocked": "依赖健康但流量不通（网络策略/连通性问题）",
    "service_target_port_mismatch": "Service targetPort 写错（Pod 正常、有端点，但连不上）",
    "node_pressure": "节点压力导致 Pod 被驱逐/无法调度",
    "quota_exhausted": "命名空间资源配额耗尽，新 Pod 创建不出来",
    "ingress_backend_invalid": "Ingress 后端指向不存在的 Service/端口",
    "limitrange_violation": "LimitRange 约束导致 Pod 无法创建",
    "image_pull": "镜像拉取失败",
    "pending_unschedulable": "调度/准入失败",
    "crashloop": "容器反复重启（CrashLoopBackOff）",
    "not_ready": "未就绪",
    "service_no_endpoints": "服务无就绪后端（Service 层故障，非 Pod 故障）",
}


def _oom_event_pods(events: list[dict[str, Any]] | None) -> set[str]:
    """从事件里找出**明确被 OOMKill** 的 Pod 名。"""
    out: set[str] = set()
    for e in events or []:
        reason = str(e.get("reason", "")).lower()
        msg = str(e.get("message", "")).lower()
        if "oomkill" in reason or "oomkill" in msg or "out of memory" in msg:
            obj = str(e.get("object", ""))
            out.add(obj.split("/")[-1])
    return out


def _probe_killed_pods(events: list[dict[str, Any]] | None) -> set[str]:
    """从事件里找出**被探针误杀**的 Pod 名（kubelet 因存活探针失败重启容器）。

    **按消息内容匹配，而不是按事件 reason。** 同一个根因在不同情况下会以
    不同 reason 出现：`Killing`（"will be restarted"）、`Unhealthy`
    （"Liveness probe failed: statuscode 404"）、`BackOff`……
    早期实现只认 reason=Killing，被一个只发 Unhealthy 事件的变体绕过了
    （由 fuzz 第二轮用例暴露）。
    """
    out: set[str] = set()
    for e in events or []:
        msg = str(e.get("message", "")).lower()
        if "liveness" in msg and any(
            k in msg for k in ("fail", "timeout", "exceeded", "unhealthy")
        ):
            out.add(str(e.get("object", "")).split("/")[-1])
    return out


def classify_pod(
    p: PodInfo,
    *,
    oom_evidenced: bool = False,
    probe_killed: bool = False,
) -> str | None:
    """把一个 Pod 归入**唯一一个**故障类别；健康返回 None。

    为什么要"唯一"：一个镜像拉取失败的 Pod 同时满足 `phase == Pending`，
    如果按"多个规则各自独立匹配"来聚合，就会被同时算成 pending 与 image_pull，
    从而误报成"多根因"。先逐 Pod 归类、再对类别去重，才能正确区分
    "一个故障"与"真的多个故障"。

    **OOM 检查刻意排在"是否 Ready"之前。** 一个 Pod 可能刚被 OOMKill 又重启成功、
    当前显示 Ready——但"刚被杀过"本身就是有诊断意义的信号。早期实现把 Ready 放在
    最前面，导致真实集群上出现过**漏报**：4 个 Pod 全部 lastExit=137，
    结论却是"当前健康"。

    已知局限：K8s 的 `lastState.terminated` 没有时间戳参与判断，
    因此很久以前 OOM 过一次、之后一直正常的 Pod 也会被标记。
    对诊断工具而言，宁可多提示一次，也不要漏掉正在发生的故障。
    """
    # ① 正向 OOM 证据最硬：K8s 会显式给出 terminated.reason=OOMKilled，
    #    或产生一条 OOMKilling 事件。
    if p.termination_reason == "OOMKilled" or oom_evidenced:
        return "oom_killed"

    # ② 有探针误杀证据、且没有 OOM 证据 → 137 只是 SIGKILL 的通用退出码，
    #    **不能据此判 OOM**。否则会建议"加大内存"，而问题其实出在探针配置上。
    #    这条修正由 fuzz 生成用例 fuzz-001/fuzz-004 逼出来。
    if (probe_killed or _pod_reason(p, "liveness probe failed")) and not oom_evidenced:
        # 独立类别，**不能复用 not_ready**：not_ready 是"兜底症状"，
        # 在存在其他类别时会被丢掉（见 dominant_categories）。探针误杀是**根因级**
        # 判断，必须能独立成立——否则会被并存的 OOM 类 Pod 掩盖掉。
        return "probe_kill"

    # ③ 兜底：只有退出码 137、没有任何其他证据时，按 OOM 处理（保持向后兼容）
    if p.last_exit_code == 137 or _pod_reason(p, "OOMKilled"):
        return "oom_killed"

    if p.healthy and p.phase == "Running":
        return None
    if p.phase == "Unknown" or _pod_reason(p, "NodeNotReady", "NodeLost"):
        return "node_failure"
    if _pod_reason(p, "ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
        return "image_pull"
    if p.phase == "Pending" or _pod_reason(p, "Unschedulable", "Insufficient", "exceeded quota"):
        return "pending_unschedulable"
    if _pod_reason(p, "CrashLoopBackOff"):
        return "crashloop"
    return "not_ready"


# 日志里"连不上某个地址"的形态。用于提取 host:port 并与配置交叉比对。
CONNECT_FAIL_HINTS = (
    "connection refused", "connection reset", "no such host",
    "i/o timeout", "econnrefused", "etimedout", "dial tcp",
    "connect:", "cannot connect", "could not connect", "failed to connect",
)
_ADDR_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\.?([A-Za-z0-9._-]*):(\d{2,5})")


@dataclass
class ConnectivitySignal:
    """一条"应用连不上 X"的证据，以及它与配置/实际端口的对账结果。"""

    host: str
    port: int | None
    log_line: str
    port_text: str = ""          # 端口原文，避免 "0000" 被 int() 显示成 "0"
    env_name: str = ""
    env_value: str = ""
    service_ports: list[int] = field(default_factory=list)

    @property
    def port_mismatch(self) -> bool:
        """日志里的端口与 Service 实际暴露的端口不一致 → 配置写错了。"""
        return bool(self.port and self.service_ports and self.port not in self.service_ports)

    @property
    def addr(self) -> str:
        raw = self.port_text or (str(self.port) if self.port is not None else "")
        return f"{self.host}:{raw}" if raw else self.host

    def render(self) -> str:
        base = self.addr
        if self.env_name:
            base += f"（来自 env {self.env_name}={self.env_value}）"
        if self.service_ports:
            base += f"；Service 实际端口={self.service_ports}"
        return base


def extract_connect_signals(log_blob: str | None) -> list[ConnectivitySignal]:
    """从日志里提取"连不上某个主机:端口"的信号（按 host:port 去重，最多 3 条）。"""
    blob = log_blob or ""
    if not blob:
        return []
    found: dict[str, ConnectivitySignal] = {}
    for line in blob.splitlines():
        low = line.lower()
        if not any(h in low for h in CONNECT_FAIL_HINTS):
            continue
        for m in _ADDR_RE.finditer(line):
            host, port = m.group(1), int(m.group(3))
            if host.replace(".", "").isdigit():      # 纯 IP 也保留，但不是重点
                pass
            key = f"{host}:{port}"
            if key in found:
                continue
            found[key] = ConnectivitySignal(
                host=host, port=port, port_text=m.group(3), log_line=line.strip()[:200]
            )
            if len(found) >= 3:
                return list(found.values())
    return list(found.values())


# 依赖不可达的信号。任务：区分"应用自己坏了"与"应用连不上外部依赖"。
# 后者重启本工作负载**毫无作用**，属于典型假阳性动作。
DEPENDENCY_FAILURE_PATTERNS = (
    "connection refused",
    "connection reset",
    "connection timed out",
    "no such host",
    "i/o timeout",
    "econnrefused",
    "etimedout",
    "dial tcp",
    "could not connect",
    "failed to connect",
    "cannot connect to",
    "context deadline exceeded",
)

# 应用"其实启动成功了"的信号。用于区分"应用故障"与"探针配置过严"。
STARTED_OK_PATTERNS = (
    "listening on",
    "now listening",
    "server started",
    "started successfully",
    "startup complete",
    "ready to serve",
    "serving on",
    "http server is running",
)


# 致命行标记：这些行代表进程"就此终止"的原因
FATAL_LINE_PATTERNS = ("fatal", "panic", "segmentation violation", "cannot continue",
                       "exit status", "unrecoverable")

# 判断"本地资源出了问题"的标记。与依赖失败相对：这类问题重启/回滚**是**有效的。
LOCAL_ISSUE_PATTERNS = ("config", "routing table", "permission denied", "no space left",
                        "invalid", "missing required", "parse", "unmarshal", "certificate",
                        "state.db", "segment")


def _match_line(blob: str, patterns: tuple[str, ...]) -> str | None:
    """在日志里找出第一个命中任一模式的行，返回该行（截断）作为证据。"""
    if not blob:
        return None
    for line in blob.splitlines():
        low = line.lower()
        if any(p in low for p in patterns):
            return line.strip()[:220]
    return None


def _last_fatal_line(log_blob: str) -> str | None:
    """返回**最后一条致命行**（进程终止的原因）。没有致命行则返回 None。"""
    fatal = [
        ln for ln in (log_blob or "").splitlines()
        if any(p in ln.lower() for p in FATAL_LINE_PATTERNS)
    ]
    return fatal[-1] if fatal else None


def detect_dependency_failure(log_blob: str | None) -> str | None:
    """若日志显示"连不上外部依赖"，返回证据行；否则 None。

    **只在"终止原因"这一行上判断**，而不是扫描全文找关键词——
    这是被真实用例逼出来的修正：日志里一条无关的非致命告警
    （如 `metrics flush failed: connection refused`）会让全文扫描误判成
    "依赖故障、重启无用"，而真正的终止原因是本地的路由表解析失败，
    回滚本来是能修好的。

    规则：
    - 有致命行 → **只看致命行**（它才是进程终止的原因）
    - 没有致命行 → 退化为扫描全文（例如进程被反复重启、日志里只有重试信息）
    """
    blob = log_blob or ""
    if not blob:
        return None

    fatal = _last_fatal_line(blob)
    if fatal is not None:
        low = fatal.lower()
        if any(p in low for p in DEPENDENCY_FAILURE_PATTERNS):
            return fatal.strip()[:220]
        # 致命行不是依赖问题 → 认为根因在工作负载内部
        return None

    return _match_line(blob, DEPENDENCY_FAILURE_PATTERNS)


def detect_local_failure(log_blob: str | None) -> str | None:
    """日志是否指向**本地**可修问题（配置/权限/磁盘等）。仅用于补充说明。"""
    return _match_line(log_blob or "", LOCAL_ISSUE_PATTERNS)


def detect_started_ok(log_blob: str | None) -> str | None:
    """若日志显示"应用已成功启动并开始服务"，返回证据行；否则 None。"""
    return _match_line(log_blob or "", STARTED_OK_PATTERNS)


def dominant_categories(
    pods: list[PodInfo], events: list[dict[str, Any]] | None = None
) -> list[str]:
    """汇总出本工作负载实际出现的**根因**类别，按 CATEGORY_ORDER 排序。

    `not_ready` 是兜底**症状**，不是独立根因。滚动更新中 OOM 的 Pod 会被重建，
    重建期间的新 Pod 因探针未通过而"未就绪"——这是 OOM 的**结果**，不是第二个根因。
    早期实现在真实集群上把这种情况报成了"2 个并存的根因"，具有误导性，
    因此这里在存在更具体类别时把 not_ready 剔除。
    """
    oom_pods = _oom_event_pods(events)
    probe_killed = _probe_killed_pods(events)

    # 探针是**工作负载级**配置：若存在探针误杀证据且完全没有 OOM 证据，
    # 那么其余同样以 137 退出、但自己没留下事件的 Pod，更可能是同一个原因。
    # 否则会把同一故障拆成 "probe_kill + oom_killed"，误报成多根因。
    if probe_killed and not oom_pods:
        probe_killed = probe_killed | {
            p.name
            for p in pods
            if p.last_exit_code == 137 and p.termination_reason != "OOMKilled"
        }
    found = {
        c
        for c in (
            classify_pod(
                p, oom_evidenced=p.name in oom_pods, probe_killed=p.name in probe_killed
            )
            for p in pods
        )
        if c
    }
    if len(found) > 1:
        found.discard("not_ready")
    return [c for c in CATEGORY_ORDER if c in found]


def _is_pending(pods: list[PodInfo]) -> bool:
    """判断是否存在调度/准入失败。

    刻意**以 Pod phase 为准**，而不是去匹配报错文本——后者是脆弱的：
    不同失败原因（资源不足、节点选择器、污点、ResourceQuota 超限……）措辞各异，
    靠关键词枚举必然漏判。这条修正正是由评测集用例 sched-004 暴露出来的。
    """
    if any(p.phase == "Pending" for p in pods):
        return True
    return _has_reason(pods, "Unschedulable", "Insufficient", "exceeded quota", "FailedScheduling")


class RuleBasedPlanner:
    """确定性诊断。零外部依赖，是系统的降级底座。"""

    name = "rule-based"

    def __init__(self, k8s: K8sClient):
        self.k8s = k8s

    # ------------------------------------------------------------------ 主入口

    def diagnose(
        self,
        namespace: str,
        workload: str,
        kind: str = "deployment",
        extra_context: str | None = None,
        final_note: str | None = None,
    ) -> Diagnosis:
        # 规则引擎是确定性的：它一次性把所有需要的证据都自己取了，
        # 因此忽略多轮上下文（保留参数只为满足 Planner 协议）。
        diag = Diagnosis(namespace=namespace, workload=workload, kind=kind, conclusion="")

        try:
            wl = self.k8s.read_workload(namespace, kind, workload)
        except Exception as exc:  # noqa: BLE001
            diag.conclusion = f"无法读取 {kind}/{workload}：{exc}"
            diag.confidence = "low"
            return diag

        selector = (wl.spec.selector.match_labels or {}) if wl.spec.selector else {}
        sel = ",".join(f"{k}={v}" for k, v in selector.items())
        try:
            pods = self.k8s.list_pods(namespace, label_selector=sel)
        except K8sUnavailable as exc:
            diag.conclusion = f"无法列出 Pod：{exc}"
            diag.confidence = "low"
            return diag

        desired = int(wl.spec.replicas or 0)
        diag.findings.append(f"期望副本 {desired}，实际 Pod {len(pods)}")

        unhealthy = [p for p in pods if not p.healthy]
        diag.evidence.append(
            Evidence(
                kind="state",
                ref=f"{kind}/{workload}",
                detail=(
                    f"{len(unhealthy)}/{len(pods)} 个 Pod 不健康"
                    if unhealthy
                    else f"{len(pods)}/{len(pods)} 个 Pod 全部就绪"
                ),
                source="kubectl get pods",
            )
        )

        # ---- 配额耗尽：Pod 创建不出来的常见集群级根因 ----
        # **必须排在 no_pods 之前**：否则"一个 Pod 都没有"会被直接归为 no_pods，
        # 而真正原因是配额满——处置完全不同（重启多少次都会被配额拒绝）。
        if not pods or all(p.phase in ("Pending", "") for p in pods):
            quotas = self._check_quota(namespace)
            if quotas:
                return self._sig_quota_exhausted(diag, quotas, workload, kind, namespace)

        # ---- 集群/节点层：LimitRange 约束让 Pod 直接被拒 ----
        # 与配额同理，必须在 no_pods 之前——否则"建不出来"会被笼统归为 no_pods。
        lr = self._check_limitrange(namespace, kind, workload)
        if lr:
            return self._sig_limitrange_violation(diag, lr, workload, kind)

        if not pods:
            return self._sig_no_pods(diag, workload, kind, desired)

        # 取事件：OOMKilling / Killing-by-liveness-probe 这类**正向证据**
        # 比退出码更权威（137 是所有 SIGKILL 的通用退出码）。
        try:
            events = self.k8s.list_events(namespace, involved_name=workload)
        except Exception:  # noqa: BLE001
            events = []

        # ---- 集群/节点层优先：节点有压力时，Pod 的异常多半是结果而非原因 ----
        pressured = self._check_node_pressure(pods)
        if pressured:
            return self._sig_node_pressure(diag, pressured, workload, kind)

        # 先"逐 Pod 归类"，再"对类别去重"，从而区分单根因与真的多根因
        categories = dominant_categories(pods, events)

        if len(categories) > 1:
            return self._sig_multi(diag, categories, pods, workload, kind, namespace)
        if len(categories) == 1:
            cat = categories[0]
            # 这两个不走 dict 分发表：它们的入参形状与其余特征不同
            if cat == "node_failure":
                return self._sig_node_failure(diag, pods, workload, kind, namespace)
            if cat in ("not_ready", "probe_kill"):
                return self._sig_notready(
                    diag, unhealthy, workload, kind, namespace, signature=cat
                )
            return {
                "oom_killed": self._sig_oom,
                "image_pull": self._sig_image,
                "pending_unschedulable": self._sig_pending,
                "crashloop": self._sig_crashloop,
            }[cat](diag, pods, workload, kind, namespace)

        # 所有 Pod 都正常 —— 但这不等于"服务可用"。
        # **必须再往上一层看 Service/Endpoints**：Pod 健康 + 无就绪后端，
        # 是一类完全不同的故障（selector/targetPort 配错），重启 Pod 毫无作用。
        # 这条检查来自 fuzz 生成用例暴露的头号盲区（详见 docs/rule-blindspots.md）。
        broken = self._services_without_endpoints(namespace, kind, workload)
        if broken:
            return self._sig_service_no_endpoints(
                diag, broken, pods, workload, kind, namespace
            )

        # Pod 健康 ≠ 业务可用。再查一步：日志里有没有"连不上依赖"。
        # 这一条来自 ITBench 真实数据——Pod 全绿但配置指错地址的场景报不出健康。
        # 流量入口：Ingress 后端指向不存在的 Service/端口
        ing = self._check_ingress_backend(namespace, workload, kind)
        if ing:
            return self._sig_ingress_backend_invalid(diag, ing, workload, kind)

        # Service 端口映射：Pod 正常、Endpoints 非空，但 targetPort 指错
        tp = self._check_target_port(namespace, kind, workload)
        if tp:
            return self._sig_target_port_mismatch(diag, tp, workload, kind, namespace)

        verdict = self._healthy_connectivity(namespace, kind, workload, pods)
        if verdict:
            return self._apply_connectivity(
                diag, verdict, workload, kind, namespace, pods_healthy=True
            )

        diag.conclusion = f"{kind}/{workload} 当前健康：{len(pods)} 个 Pod 全部就绪。"
        diag.confidence = "high"
        diag.matched_signature = "healthy"
        return diag

    def _services_without_endpoints(
        self, namespace: str, kind: str, workload: str
    ) -> list[dict[str, Any]]:
        """返回"选中该工作负载、但一个就绪后端都没有"的 Service。

        只在 Pod 侧没查出问题时才调用——否则会把 Pod 故障重复报成 Service 故障。
        """
        try:
            wl = self.k8s.read_workload(namespace, kind, workload)
            tmpl = getattr(wl.spec, "template", None)
            labels = dict(tmpl.metadata.labels or {}) if tmpl is not None else {}
            if not labels:
                return []
            services = self.k8s.services_selecting(namespace, labels)
            if not services:
                return []          # 没有 Service 指向它，无从谈起
            endpoints = {e["name"]: e for e in self.k8s.list_endpoints(namespace)}
            return [
                {**svc, "ready": endpoints.get(svc["name"], {}).get("ready", 0)}
                for svc in services
                if endpoints.get(svc["name"], {}).get("ready", 0) == 0
            ]
        except Exception:  # noqa: BLE001
            # 取不到 endpoints（权限不足/API 不可用）时不应误报，静默跳过
            return []

    def _sig_service_no_endpoints(
        self,
        diag: Diagnosis,
        broken: list[dict[str, Any]],
        pods: list[PodInfo],
        workload: str,
        kind: str,
        ns: str,
    ) -> Diagnosis:
        """Service 层故障：Pod 正常，但 Service 没有就绪后端。"""
        diag.matched_signature = "service_no_endpoints"
        diag.confidence = "high"
        names = "、".join(b["name"] for b in broken)
        diag.conclusion = (
            f"{len(pods)} 个 Pod **全部就绪**，但指向该工作负载的 Service（{names}）"
            f"**没有任何就绪后端**（Endpoints 为空）——业务实际不可达。\n\n"
            f"这**不是 Pod 故障**：Pod 本身健康。最可能是 Service 的 selector 与 Pod 的 "
            f"label 不匹配，或 targetPort 写错。\n\n"
            f"**重启或回滚 Pod 都不会有任何作用**——label 依旧不匹配，问题原样复现。"
            f"需要修改 Service 定义，而当前工具白名单里没有「改 Service」的动作，"
            f"因此建议人工介入。"
        )
        diag.evidence.append(
            Evidence("state", f"{kind}/{workload}",
                     f"{len(pods)}/{len(pods)} 个 Pod 就绪（Pod 侧无异常）", "kubectl get pods")
        )
        for b in broken:
            diag.evidence.append(
                Evidence("state", f"Service/{b['name']}",
                         f"selector={b['selector']} 就绪后端=0", "kubectl get endpoints")
            )
            diag.evidence.append(
                Evidence("state", f"Service/{b['name']} 的 selector 与 Pod label 对比",
                         f"selector 要求 {b['selector']}；Pod 实际 label 见上方工作负载",
                         "kubectl get svc -o yaml")
            )
        diag.findings.append(
            "⛔ 已拦截重启/回滚建议：根因在 Service 定义，Pod 侧动作无效"
        )
        diag.findings.append(
            "👉 白名单里没有修改 Service 的动作（这是刻意的边界），请人工修正 selector 或 targetPort"
        )
        # 只给只读取证动作，引导人工确认；不给任何变更动作
        diag.candidates = [
            Candidate(
                "get_endpoints",
                {"namespace": ns, "name": broken[0]["name"]},
                "确认 Service 当前的后端端点列表，验证是否真的为空。",
                note="只读动作，自动放行。",
            )
        ]
        return diag

    # ------------------------------------------------------------ 新增特征

    def _sig_node_failure(
        self, diag: Diagnosis, pods: list[PodInfo], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        """节点级故障。

        关键判断：这不是工作负载自身的问题，**对它做滚动重启或回滚都没有意义**，
        甚至会加剧抖动（本该被驱逐的 Pod 被反复重建）。
        """
        diag.matched_signature = "node_failure"
        diag.confidence = "medium"
        victims = [p for p in pods if classify_pod(p) == "node_failure"]
        nodes = sorted({p.node for p in victims if p.node})
        diag.conclusion = (
            f"有 {len(victims)} 个 Pod 处于 **Unknown / NodeNotReady** 状态"
            + (f"（节点：{', '.join(nodes)}）" if nodes else "")
            + "。根因在**节点层**而非工作负载本身——重启或回滚本工作负载无效，"
            "且可能引发大规模抖动。应升级为集群级事件处理。"
        )
        for p in victims[:3]:
            diag.evidence.append(
                Evidence(
                    "state",
                    f"Pod/{p.name}",
                    f"phase={p.phase} reason={p.reason or 'NodeNotReady'} node={p.node}",
                    "kubectl get pods",
                )
            )
            diag.evidence.append(
                Evidence(
                    "state",
                    f"Node/{p.node}",
                    "节点不可达，Pod 状态为 Unknown",
                    "kubectl get nodes",
                )
            )
        diag.findings.append(
            "⚠️ 集群级故障：不应逐工作负载重启，建议先确认节点状态、必要时 cordon 并等待重调度"
        )
        # 唯一可接受的动作是节点级隔离，且必须人工确认
        diag.candidates.append(
            Candidate(
                "cordon_node",
                {"node": nodes[0]} if nodes else {},
                "先将异常节点标记为不可调度，避免新 Pod 继续调度上去。",
                note="这是遏制手段，不是根因修复。节点恢复后需 uncordon。",
            )
        )
        return diag

    # ---------------------------------------------------------- 配置层

    def _config_mismatch(
        self, ns: str, kind: str, workload: str, log_blob: str | None
    ) -> ConnectivitySignal | None:
        """把"日志里连不上的地址"与"env 配置"和"Service 真实端口"做交叉比对。

        只有当**日志端口与 Service 实际端口不一致**时才判定为配置错误——
        单纯连不上可能只是依赖挂了，那不该归咎于本工作负载的配置。
        """
        signals = extract_connect_signals(log_blob)
        if not signals:
            return None
        try:
            env = {e["name"]: e["value"] for e in self.k8s.workload_env(ns, kind, workload)}
        except Exception:  # noqa: BLE001
            env = {}
        try:
            services = {s["name"]: s for s in self.k8s.list_services(ns)}
        except Exception:  # noqa: BLE001
            services = {}

        for sig in signals:
            for name, value in env.items():
                if sig.host and sig.host in (value or ""):
                    sig.env_name, sig.env_value = name, value
                    break
            svc = services.get(sig.host)
            if svc:
                sig.service_ports = [pt["port"] for pt in svc["ports"]]
            if sig.port_mismatch:
                return sig
        return None

    # env 里出现 host:port 的形态（如 KAFKA_ADDR=kafka:9999）
    _ENV_ADDR_RE = re.compile(r"([A-Za-z][A-Za-z0-9._-]*):(\d{2,5})")

    def _config_port_conflict(
        self, ns: str, kind: str, workload: str
    ) -> ConnectivitySignal | None:
        """不依赖日志的降级检查：**env 指向的端口在对应 Service 上不存在**。

        为什么需要这条：日志不一定拿得到（已轮转、采集未接入、或像本项目
        评测环境那样只有对象快照）。但"配置里写的端口没有任何 Service 在监听"
        本身就是强信号——不需要等应用报错。

        仍然要求 host 是**本命名空间真实存在的 Service**，否则不判——
        避免把指向集群外地址的配置误判成错误。
        """
        try:
            env = {e["name"]: e["value"] for e in self.k8s.workload_env(ns, kind, workload)}
        except Exception:  # noqa: BLE001
            return None
        if not env:
            return None
        try:
            services = {s["name"]: [p["port"] for p in s["ports"]]
                        for s in self.k8s.list_services(ns)}
        except Exception:  # noqa: BLE001
            return None

        for name, value in env.items():
            for m in self._ENV_ADDR_RE.finditer(value or ""):
                host, port = m.group(1), int(m.group(2))
                ports = services.get(host)
                if ports and port not in ports:
                    return ConnectivitySignal(
                        host=host, port=port, port_text=m.group(2), log_line="",
                        env_name=name, env_value=value, service_ports=ports,
                    )
        return None

    # ------------------------------------------------- 服务层 / 节点层

    def _check_target_port(self, ns: str, kind: str, workload: str) -> dict[str, Any] | None:
        """Service 的 targetPort 与容器实际端口对不上。

        这一层专门覆盖"Pod 全就绪、Endpoints 也非空，但流量全被拒"——
        fuzz 生成用例发现我的 `endpoints == 0` 判据抓不到这种情况。
        """
        try:
            wl = self.k8s.read_workload(ns, kind, workload)
        except Exception:  # noqa: BLE001
            return None
        tmpl = getattr(wl.spec, "template", None)
        if tmpl is None:
            return None
        labels = dict(tmpl.metadata.labels or {})
        # 容器声明/探针使用的端口集合 = 应用真正在听的端口
        app_ports: set[int] = set()
        for c in (getattr(tmpl.spec, "containers", None) or []):
            for p in (getattr(c, "ports", None) or []):
                if p.container_port:
                    app_ports.add(int(p.container_port))
            for probe in (getattr(c, "readiness_probe", None),
                          getattr(c, "liveness_probe", None),
                          getattr(c, "startup_probe", None)):
                if probe is not None:
                    hp = getattr(probe, "http_get", None) or getattr(probe, "tcp_socket", None)
                    if hp is not None and getattr(hp, "port", None):
                        try:
                            app_ports.add(int(hp.port))
                        except (TypeError, ValueError):
                            pass
        if not app_ports:
            return None
        try:
            services = self.k8s.services_selecting(ns, labels)
        except Exception:  # noqa: BLE001
            return None
        for svc in services:
            for pt in svc.get("ports") or []:
                tp = pt.get("target_port")
                try:
                    tp_int = int(tp)
                except (TypeError, ValueError):
                    continue          # 命名端口无法静态判断，跳过
                if tp_int not in app_ports:
                    return {"service": svc["name"], "target_port": tp_int,
                            "app_ports": sorted(app_ports), "port": pt.get("port")}
        return None

    def _sig_target_port_mismatch(
        self, diag: Diagnosis, info: dict[str, Any], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        diag.matched_signature = "service_target_port_mismatch"
        diag.confidence = "high"
        diag.conclusion = (
            f"{kind}/{workload} 的 Pod 正常、Endpoints 也非空，但流量连不上。\n\n"
            f"**Service/{info['service']} 的 targetPort={info['target_port']}，"
            f"而容器实际监听的是 {info['app_ports']}** —— 端口转发到了没人监听的地方。\n\n"
            f"这是 **Service 定义的问题**，不是 Pod 故障：重启/回滚 Pod 都不会改变端口映射。"
            f"需要修正 Service 的 targetPort，而当前白名单里没有「改 Service」的动作"
            f"（刻意的边界），建议人工介入。"
        )
        diag.evidence.append(
            Evidence("config", f"Service/{info['service']}",
                     f"targetPort={info['target_port']}（对外 port={info['port']}）",
                     "kubectl get svc -o yaml")
        )
        diag.evidence.append(
            Evidence("config", f"{kind}/{workload}",
                     f"容器声明/探针使用的端口={info['app_ports']}", "kubectl get deploy -o yaml")
        )
        diag.findings.append("⛔ 已拦截 Pod 侧动作：根因在 Service 的端口映射")
        diag.candidates = [
            Candidate("get_endpoints", {"namespace": ns, "name": info["service"]},
                      "确认 Service 的后端端点，验证端口映射问题。", note="只读动作，自动放行。")
        ]
        return diag

    def _check_node_pressure(self, pods: list[PodInfo]) -> list[dict[str, Any]]:
        """节点是否存在会让 Pod 被驱逐/无法调度的压力条件。"""
        try:
            nodes = {n["name"]: n for n in self.k8s.list_nodes()}
        except Exception:  # noqa: BLE001
            return []
        involved = {p.node for p in pods if p.node}
        return [nodes[n] for n in involved if n in nodes and nodes[n].get("pressured")]

    def _sig_node_pressure(
        self, diag: Diagnosis, pressured: list[dict[str, Any]], workload: str, kind: str
    ) -> Diagnosis:
        names = "、".join(f"{n['name']}({','.join(n['pressured'])})" for n in pressured)
        diag.matched_signature = "node_pressure"
        diag.confidence = "high"
        diag.conclusion = (
            f"{kind}/{workload} 的异常与**节点压力**有关：{names}。\n\n"
            f"节点处于压力状态时，kubelet 会驱逐 Pod、并把节点标记为不可调度——"
            f"这是**集群级问题，不是本工作负载的问题**。\n\n"
            f"对本工作负载重启/回滚**无效**，且可能加剧抖动。"
            f"应先处理节点压力（磁盘/内存/PID 耗尽），必要时 cordon 隔离。"
        )
        for n in pressured:
            diag.evidence.append(
                Evidence("state", f"Node/{n['name']}", f"压力条件={n['pressured']}",
                         "kubectl describe node")
            )
        diag.findings.append("⛔ 已拦截本工作负载的变更建议：根因在节点层")
        return diag

    def _check_ingress_backend(self, ns: str, workload: str, kind: str) -> list[dict[str, Any]]:
        """Ingress 的后端 Service/端口是否真实存在。

        流量入口写错时，Pod 侧一切正常但外部访问全断——属于服务/网络层。
        """
        try:
            ingresses = self.k8s.list_ingresses(ns)
            services = {s["name"]: s for s in self.k8s.list_services(ns)}
        except Exception:  # noqa: BLE001
            return []
        broken = []
        for ing in ingresses:
            for b in ing.get("backends") or []:
                svc = services.get(b["service"])
                if svc is None:
                    broken.append({**b, "ingress": ing["name"], "reason": "Service 不存在"})
                    continue
                ports = [pt["port"] for pt in svc["ports"]]
                if isinstance(b["port"], int) and b["port"] not in ports:
                    broken.append({**b, "ingress": ing["name"],
                                   "reason": f"Service 实际端口 {ports}"})
        return broken

    def _sig_ingress_backend_invalid(
        self, diag: Diagnosis, broken: list[dict[str, Any]], workload: str, kind: str
    ) -> Diagnosis:
        b = broken[0]
        diag.matched_signature = "ingress_backend_invalid"
        diag.confidence = "medium"
        diag.conclusion = (
            f"Ingress/{b['ingress']} 的后端指向 **{b['service']}:{b['port']}**，"
            f"但该后端不可用（{b['reason']}）。\n\n"
            f"这是**流量入口配置问题**，Pod 侧完全正常——重启/回滚 Pod 都不会让外部访问恢复。"
            f"需要修正 Ingress 的后端定义（白名单里没有该动作，请人工处理）。"
        )
        diag.evidence.append(
            Evidence("config", f"Ingress/{b['ingress']}",
                     f"backend={b['service']}:{b['port']}（{b['reason']}）",
                     "kubectl get ingress")
        )
        diag.findings.append("⛔ 已拦截 Pod 侧动作：根因在 Ingress 定义")
        diag.candidates = [
            Candidate("get_endpoints", {"namespace": diag.namespace, "name": b["service"]},
                      "确认后端 Service 是否存在及其端点。", note="只读动作，自动放行。")
        ]
        return diag

    def _check_limitrange(self, ns: str, kind: str, workload: str) -> list[dict[str, Any]]:
        """LimitRange 是否会使该工作负载的 Pod 无法创建。

        典型：LimitRange 设了 max，而工作负载的 requests 超过它 → Pod 直接被拒绝。
        """
        try:
            ranges = self.k8s.list_limitranges(ns)
            wl = self.k8s.read_workload(ns, kind, workload)
        except Exception:  # noqa: BLE001
            return []
        if not ranges:
            return []
        tmpl = getattr(wl.spec, "template", None)
        if tmpl is None:
            return []
        reqs: list[tuple[str, str]] = []
        for c in (getattr(tmpl.spec, "containers", None) or []):
            res = getattr(c, "resources", None)
            for k, v in ((res.requests or {}) if res and res.requests else {}).items():
                reqs.append((k, str(v)))
            for k, v in ((res.limits or {}) if res and res.limits else {}).items():
                reqs.append((k, str(v)))
        if not reqs:
            return []
        violations = []
        for lr in ranges:
            for item in lr["limits"]:
                maxv = item.get("max") or {}
                for k, v in reqs:
                    if k in maxv and not _quantity_le(v, maxv[k]):
                        violations.append({"limitrange": lr["name"], "resource": k,
                                           "workload_value": v, "max": maxv[k]})
        return violations

    def _sig_limitrange_violation(
        self, diag: Diagnosis, violations: list[dict[str, Any]], workload: str, kind: str
    ) -> Diagnosis:
        v = violations[0]
        diag.matched_signature = "limitrange_violation"
        diag.confidence = "high"
        diag.conclusion = (
            f"{kind}/{workload} 声明了 {v['resource']}={v['workload_value']}，"
            f"但 LimitRange/{v['limitrange']} 的上限是 {v['max']} —— **超出上限，Pod 会被直接拒绝创建**。\n\n"
            f"这是**命名空间策略问题**：重启/扩容都会被拒。需要下调资源声明，"
            f"或由平台方调整 LimitRange。"
        )
        diag.evidence.append(
            Evidence("config", f"LimitRange/{v['limitrange']}",
                     f"max {v['resource']}={v['max']}", "kubectl get limitrange")
        )
        diag.evidence.append(
            Evidence("config", f"{kind}/{workload}",
                     f"{v['resource']}={v['workload_value']}", "kubectl get deploy -o yaml")
        )
        diag.findings.append("⛔ 已拦截重启建议：声明超限时重建同样会被拒")
        return diag

    def _configmap_rollback_candidate(
        self, ns: str, kind: str, workload: str
    ) -> dict[str, Any] | None:
        """该工作负载挂载/引用的 ConfigMap 是否有可回滚的历史版本。

        K8s 原生不保留 ConfigMap 历史，版本由本项目的 ConfigMapHistory 记录——
        这正是"配置层能回滚"的前提。
        """
        from .knowledge import ConfigMapHistory

        try:
            names = self.k8s.workload_configmaps(ns, kind, workload)
        except Exception:  # noqa: BLE001
            return None
        if not names:
            return None
        hist = ConfigMapHistory()
        for name in names:
            prev = hist.previous(ns, name)
            if prev is not None:
                return {"name": name, "data": prev, "versions": hist.version_count(ns, name)}
        return None

    def _check_quota(self, ns: str) -> list[dict[str, Any]]:
        try:
            return [q for q in self.k8s.list_resourcequotas(ns) if q.get("exhausted")]
        except Exception:  # noqa: BLE001
            return []

    def _sig_quota_exhausted(
        self, diag: Diagnosis, quotas: list[dict[str, Any]], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        q = quotas[0]
        detail = "、".join(f"{k}={q['used'].get(k)}/{q['hard'].get(k)}" for k in q["exhausted"][:3])
        diag.matched_signature = "quota_exhausted"
        diag.confidence = "high"
        diag.conclusion = (
            f"{kind}/{workload} 的 Pod 创建不出来，根因是**命名空间的资源配额已耗尽**："
            f"Quota/{q['name']} 的 {detail} 已达上限。\n\n"
            f"这是**命名空间级问题**：重启/回滚本工作负载都会被配额拒绝。"
            f"需要提高配额、或释放其他工作负载占用的资源。"
        )
        diag.evidence.append(
            Evidence("config", f"ResourceQuota/{q['name']}", f"已耗尽项：{detail}",
                     "kubectl get resourcequota")
        )
        diag.findings.append("⛔ 已拦截重启建议：配额耗尽时重建 Pod 同样会被拒绝")
        diag.candidates = [
            Candidate("scale_workload",
                      {"namespace": ns, "name": workload, "kind": kind, "replicas": 0},
                      "先把副本数降到 0 释放配额，再与平台方确认配额调整。",
                      note="⚠️ 会中断服务；仅在确认可接受后执行。"),
        ]
        return diag

    def _diagnose_connectivity(
        self, ns: str, kind: str, workload: str, log_blob: str | None
    ) -> tuple[str, ConnectivitySignal] | None:
        """把"连不上某个依赖"归因到**正确的层**。

        这是值班最常问的问题：**"我连不上 kafka，是 kafka 挂了还是我配错了？"**
        三种可能，处置完全不同：

        1. 端口对不上 → 我们配错了 → ``config_misconfiguration``（回滚配置）
        2. 端口对、但对方 Service 没有就绪后端 → **对方挂了** → ``dependency_unavailable``
           （本工作负载不该动手，等依赖恢复）
        3. 端口对、对方有就绪后端 → 网络层挡了 → ``network_blocked``
           （查 NetworkPolicy / DNS / 连通性）

        **注意**：这里必须直接从**原始日志信号**出发做归因，
        而不能依赖 ``_config_mismatch``（它只在"确实不匹配"时才返回）。
        否则"端口对得上"的情况会被整个丢掉，依赖层永远判不出来。
        """
        try:
            env = {e["name"]: e["value"] for e in self.k8s.workload_env(ns, kind, workload)}
        except Exception:  # noqa: BLE001
            env = {}
        try:
            services = {s["name"]: s for s in self.k8s.list_services(ns)}
        except Exception:  # noqa: BLE001
            services = {}
        try:
            endpoints = {e["name"]: e for e in self.k8s.list_endpoints(ns)}
        except Exception:  # noqa: BLE001
            endpoints = {}

        for sig in extract_connect_signals(log_blob):
            for name, value in env.items():
                if sig.host and sig.host in (value or ""):
                    sig.env_name, sig.env_value = name, value
                    break
            svc = services.get(sig.host)
            if svc is None:
                # 不是本命名空间的 Service（可能是集群外地址），无法归因
                continue
            sig.service_ports = [pt["port"] for pt in svc["ports"]]
            if sig.port_mismatch:
                return ("config_misconfiguration", sig)
            ready = (endpoints.get(sig.host) or {}).get("ready", 0)
            if ready == 0:
                return ("dependency_unavailable", sig)
            return ("network_blocked", sig)

        # 日志里没有可用信号（拿不到日志 / 没打错误）→ 降级用"配置端口冲突"兜底
        conflict = self._config_port_conflict(ns, kind, workload)
        if conflict is not None:
            return ("config_misconfiguration", conflict)
        return None

    def _sig_dependency_unavailable(
        self, diag: Diagnosis, sig: ConnectivitySignal, workload: str, kind: str
    ) -> Diagnosis:
        """依赖方自己挂了——本工作负载不该动手。"""
        diag.matched_signature = "dependency_unavailable"
        diag.confidence = "high"
        diag.conclusion = (
            f"{kind}/{workload} 连不上 `{sig.addr}`，但**问题不在本工作负载**：\n\n"
            f"依赖方 Service `{sig.host}` 当前**没有任何就绪后端**（0 个）。\n\n"
            f"也就是说，是**对方挂了**。对本工作负载做重启、回滚、扩容**都不会有任何作用**——"
            f"依赖恢复后它会自行恢复正常。建议转交该依赖的负责方，或先确认它的 Pod 状态。"
        )
        diag.evidence.append(
            Evidence("state", f"Service/{sig.host}", "就绪后端 = 0", "kubectl get endpoints")
        )
        if sig.log_line:
            diag.evidence.append(
                Evidence("log", f"{kind}/{workload} 容器日志", sig.log_line, "kubectl logs")
            )
        diag.findings.append("⛔ 已拦截本工作负载的所有变更建议：根因在依赖方")
        diag.findings.append(f"👉 请排查 Service/{sig.host} 及其后端 Pod")
        diag.candidates = [
            Candidate(
                "get_pods",
                {"namespace": diag.namespace or "default",
                 "label_selector": f"app={sig.host}"},
                f"查看依赖方 {sig.host} 的 Pod 状态，确认它为什么没有就绪后端。",
                note="只读动作，自动放行；**建议在依赖方的工作负载上执行诊断**。",
            )
        ]
        return diag

    def _sig_network_blocked(
        self, diag: Diagnosis, sig: ConnectivitySignal, workload: str, kind: str, ns: str
    ) -> Diagnosis:
        """依赖健康、配置也对，但流量不通 → 网络层。"""
        diag.matched_signature = "network_blocked"
        diag.confidence = "medium"
        policies = []
        try:
            policies = self.k8s.list_networkpolicies(ns)
        except Exception:  # noqa: BLE001
            pass
        policy_hint = (
            f"该命名空间存在 {len(policies)} 条 NetworkPolicy（{', '.join(p['name'] for p in policies[:3])}），"
            f"**优先怀疑它阻断了出/入向流量**。"
            if policies
            else "该命名空间没有 NetworkPolicy，问题可能在更下层（DNS、CNI、对端端口监听）。"
        )
        diag.conclusion = (
            f"{kind}/{workload} 连不上 `{sig.addr}`，但**配置与依赖都正常**：\n\n"
            f"端口对得上，依赖方 `{sig.host}` 也有就绪后端。\n\n"
            f"因此问题在**网络连通性**层。{policy_hint}"
        )
        diag.evidence.append(
            Evidence("state", f"Service/{sig.host}", "端口匹配且有就绪后端，配置无误",
                     "kubectl get svc/endpoints")
        )
        if policies:
            diag.evidence.append(
                Evidence("config", "NetworkPolicy",
                         "、".join(f"{p['name']}({','.join(p['policy_types'])})" for p in policies[:5]),
                         "kubectl get networkpolicy")
            )
        diag.findings.append("⛔ 已拦截重启/回滚建议：配置与依赖均正常，动作无效")
        diag.candidates = [
            Candidate(
                "get_configmap",
                {"namespace": ns, "name": sig.host},
                "先确认依赖方的连通性与策略配置。",
                note="网络类问题通常需要人工介入，Agent 不做策略变更。",
            )
        ]
        return diag

    def _sig_config_mismatch(
        self,
        diag: Diagnosis,
        sig: ConnectivitySignal,
        workload: str,
        kind: str,
        ns: str,
        *,
        pods_healthy: bool,
    ) -> Diagnosis:
        """配置指向错误地址：Pod 侧怎么动手都没用。"""
        diag.matched_signature = "config_misconfiguration"
        # 有日志实证 → high；只有配置与端口冲突的旁证 → medium（需人工确认）
        diag.confidence = "high" if (sig.log_line and sig.env_name) else "medium"
        header = (
            f"{kind}/{workload} 的 Pod **全部就绪**，但业务可能已经不可用——"
            if pods_healthy
            else f"{kind}/{workload} 的容器启动失败，"
        )
        symptom = (
            f"日志显示应用连不上 `{sig.addr}`。\n\n"
            if sig.log_line
            else f"配置里引用了 `{sig.addr}`，但没有日志可佐证。\n\n"
        )
        diag.conclusion = (
            f"{header}{symptom}"
            + (
                f"而环境变量 **{sig.env_name}={sig.env_value}** 正是这个地址，"
                f"同名 Service 实际暴露的端口却是 **{sig.service_ports}** —— "
                f"**端口对不上，配置写错了**。\n\n"
                if sig.env_name and sig.service_ports
                else "该地址与当前配置不匹配。\n\n"
            )
            + "**这不是工作负载故障**：Pod 本身没问题，重启或扩容都不会让地址变对。\n"
            "正确做法是回滚到配置正确的上一个修订版本"
            "（env 写在 Deployment 的 pod template 里，`rollout_undo` 会连配置一起回退）；"
            "若没有可回滚的历史版本，需人工修正环境变量。"
        )
        if sig.log_line:
            diag.evidence.append(
                Evidence("log", f"{kind}/{workload} 容器日志", sig.log_line, "kubectl logs")
            )
        if sig.env_name:
            diag.evidence.append(
                Evidence("config", f"env {sig.env_name}", f"{sig.env_value}",
                         "kubectl get deploy -o jsonpath")
            )
        if sig.service_ports:
            diag.evidence.append(
                Evidence("state", f"Service/{sig.host}",
                         f"实际暴露端口 {sig.service_ports}，与配置的 {sig.addr} 不一致",
                         "kubectl get svc")
            )
        diag.findings.append("⛔ 已拦截重启/扩容建议：根因在配置，Pod 侧动作无效")
        diag.findings.append("👉 首选 rollout_undo 回退到配置正确的版本")
        # 如果该工作负载引用的 ConfigMap 有历史版本，直接给出配置回滚方案
        rb = self._configmap_rollback_candidate(ns, kind, workload)
        cm_candidate = (
            Candidate(
                "rollback_configmap",
                {"namespace": ns, "name": rb["name"], "data": rb["data"]},
                f"把 ConfigMap/{rb['name']} 回滚到上一个版本"
                f"（已记录 {rb['versions']} 个历史版本）。",
                note="⚠️ 配置层写操作，T2 强确认；请先核对回滚内容。",
            )
            if rb
            else None
        )
        diag.candidates = [
            Candidate(
                "rollout_undo",
                {"namespace": ns, "name": workload, "kind": kind},
                "回滚到上一个修订版本——env 在 pod template 里，回滚会一并恢复正确的配置。",
                note="⚠️ 重启/扩容**都无效**：配置不变，Pod 重建后照样连不上。",
            ),
            *([cm_candidate] if cm_candidate else []),
            Candidate(
                "get_configmap",
                {"namespace": ns, "name": sig.env_name or sig.host},
                "读取相关配置内容，人工确认正确地址。",
                note="只读动作，自动放行。",
            ),
        ]
        return diag

    def _apply_connectivity(
        self,
        diag: Diagnosis,
        verdict: tuple[str, ConnectivitySignal],
        workload: str,
        kind: str,
        ns: str,
        *,
        pods_healthy: bool,
    ) -> Diagnosis:
        which, sig = verdict
        if which == "config_misconfiguration":
            return self._sig_config_mismatch(diag, sig, workload, kind, ns,
                                             pods_healthy=pods_healthy)
        if which == "dependency_unavailable":
            return self._sig_dependency_unavailable(diag, sig, workload, kind)
        return self._sig_network_blocked(diag, sig, workload, kind, ns)

    def _healthy_connectivity(
        self, ns: str, kind: str, workload: str, pods: list[PodInfo]
    ) -> tuple[str, ConnectivitySignal] | None:
        """Pod 健康时也要看连通性——**这是"业务全红却报健康"的唯一拦截点**。

        先读日志；日志拿不到时归因器内部会自动退化为"配置端口冲突"检查。
        """
        logs = ""
        for p in pods[:1]:
            try:
                logs = self.k8s.pod_logs(ns, p.name, tail=40)
                break
            except Exception:  # noqa: BLE001
                continue
        return self._diagnose_connectivity(ns, kind, workload, logs)

    def _healthy_but_dependency_broken(
        self, ns: str, kind: str, workload: str, pods: list[PodInfo]
    ) -> ConnectivitySignal | None:
        """Pod 全部就绪，但日志显示连不上依赖。

        这是真实数据里占比最大的一类（配置类根因），而早期实现只看 Pod 状态，
        会直接输出"当前健康"——**业务全红却报健康**。
        """
        if not pods:
            return None
        for p in pods[:1]:
            try:
                logs = self.k8s.pod_logs(ns, p.name, tail=40)
            except Exception:  # noqa: BLE001
                continue
            sig = self._config_mismatch(ns, kind, workload, logs)
            if sig:
                return sig
        return None

    def _sig_multi(
        self,
        diag: Diagnosis,
        categories: list[str],
        pods: list[PodInfo],
        workload: str,
        kind: str,
        ns: str,
    ) -> Diagnosis:
        """多根因叠加。

        原先的实现是"首个命中即返回"，只报告一个根因，覆盖不全。
        这里把每个类别的证据与候选动作都收集起来，并按严重度排序呈现。
        """
        diag.matched_signature = "multi_root_cause"
        diag.confidence = "medium"
        labels = "、".join(CATEGORY_LABEL[c] for c in categories)
        diag.conclusion = (
            f"检测到 **{len(categories)} 个并存的根因**：{labels}。"
            f"它们需要分别处置，只修其中一个不会恢复服务。"
        )
        diag.findings.append(
            "⚠️ 多根因场景：建议按下列顺序逐个处置，每步之后重新观察"
        )

        seen_tools: set[str] = set()
        for cat in categories:
            sub = Diagnosis(namespace=ns, workload=workload, kind=kind, conclusion="")
            if cat == "oom_killed":
                sub = self._sig_oom(sub, pods, workload, kind, ns)
            elif cat == "image_pull":
                sub = self._sig_image(sub, pods, workload, kind, ns)
            elif cat == "pending_unschedulable":
                sub = self._sig_pending(sub, pods, workload, kind, ns)
            elif cat == "crashloop":
                sub = self._sig_crashloop(sub, pods, workload, kind, ns)
            elif cat == "node_failure":
                sub = self._sig_node_failure(sub, pods, workload, kind, ns)
            elif cat == "probe_kill":
                sub = self._sig_notready(
                    sub, [p for p in pods if not p.healthy], workload, kind, ns,
                    signature="probe_kill",
                )
            else:
                sub = self._sig_notready(
                    sub, [p for p in pods if not p.healthy], workload, kind, ns
                )

            for e in sub.evidence:
                e.detail = f"[{CATEGORY_LABEL[cat]}] {e.detail}"
                diag.evidence.append(e)
            for c in sub.candidates:
                if c.tool in seen_tools:
                    continue
                seen_tools.add(c.tool)
                c.rationale = f"[{CATEGORY_LABEL[cat]}] {c.rationale}"
                diag.candidates.append(c)
        return diag

    # ------------------------------------------------------------------ 特征

    def _sig_no_pods(self, diag: Diagnosis, workload: str, kind: str, desired: int) -> Diagnosis:
        diag.matched_signature = "no_pods"
        diag.confidence = "high"
        diag.conclusion = (
            f"{kind}/{workload} 期望 {desired} 个副本，但当前**一个 Pod 都没有**。"
            f"通常是调度失败、配额不足，或控制器本身异常。"
        )
        diag.evidence.append(
            Evidence("state", f"{kind}/{workload}", "status.replicas=0，无 Pod 被创建", "apps/v1")
        )
        try:
            events = self.k8s.list_events(diag.namespace, involved_name=workload)
            for e in events[:5]:
                diag.evidence.append(
                    Evidence("event", e["object"], f"{e['reason']}: {e['message'][:200]}", "events")
                )
            diag.findings.append(f"关联事件 {len(events)} 条")
        except Exception:  # noqa: BLE001
            pass
        diag.candidates.append(
            Candidate(
                "rollout_restart",
                {"namespace": diag.namespace, "name": workload, "kind": kind},
                "重启控制器以触发重新调度，观察是否能创建出 Pod。",
                note="如果根因是节点资源不足或配额限制，重启无效。",
            )
        )
        return diag

    def _sig_oom(
        self, diag: Diagnosis, pods: list[PodInfo], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        diag.matched_signature = "oom_killed"
        diag.confidence = "high"
        victim = next((p for p in pods if p.last_exit_code == 137), pods[0])
        limit = victim.memory_limit or "未设置"
        diag.conclusion = (
            f"容器因 **OOMKilled（退出码 137）** 反复重启，内存限制为 {limit}。"
            f"最可能的根因是内存 limit 设置过低，或存在内存泄漏/流量上涨。"
        )
        diag.evidence += [
            Evidence("state", f"Pod/{victim.name}", f"重启 {victim.restarts} 次", "kubectl get pods"),
            Evidence("state", f"Pod/{victim.name}", f"上次退出码 {victim.last_exit_code} (SIGKILL)", "container lastState"),
            Evidence("config", f"{kind}/{workload}", f"memory limit = {limit}", "kubectl get deploy -o yaml"),
        ]
        try:
            events = self.k8s.list_events(ns, involved_name=victim.name)
            for e in events[:3]:
                if "kill" in (e["message"] or "").lower() or e["reason"] == "Killing":
                    diag.evidence.append(
                        Evidence("event", e["object"], e["message"][:200], "events")
                    )
        except Exception:  # noqa: BLE001
            pass

        diag.candidates += [
            Candidate(
                "rollout_restart",
                {"namespace": ns, "name": workload, "kind": kind},
                "重启以清理当前异常实例，快速恢复服务。",
                note="⚠️ 只缓解不根治：如果内存 limit 不变，重启后仍会再次 OOM。",
            ),
            Candidate(
                "patch_resources",
                {
                    "namespace": ns,
                    "name": workload,
                    "container": (victim.containers or ["app"])[0],
                    "memory_limit": _double_memory(limit),
                },
                f"将内存 limit 从 {limit} 提升到 {_double_memory(limit)}，从根因上缓解。",
                note="需要评估节点剩余内存与集群配额，属于 T2 中危操作。",
            ),
        ]
        return diag

    def _sig_image(
        self, diag: Diagnosis, pods: list[PodInfo], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        diag.matched_signature = "image_pull"
        diag.confidence = "high"
        victim = next((p for p in pods if _has_reason([p], "ImagePull")), pods[0])
        diag.conclusion = (
            f"镜像拉取失败（{victim.reason}）。通常是镜像 tag 不存在、仓库凭据失效，"
            f"或网络策略阻断。**这不是重启能解决的问题**，需要修正镜像引用或凭据。"
        )
        diag.evidence += [
            Evidence("state", f"Pod/{victim.name}", f"{victim.reason}: {victim.message[:200]}", "kubectl describe"),
        ]
        try:
            events = self.k8s.list_events(ns, involved_name=victim.name)
            for e in events[:3]:
                diag.evidence.append(
                    Evidence("event", e["object"], f"{e['reason']}: {e['message'][:200]}", "events")
                )
        except Exception:  # noqa: BLE001
            pass

        diag.candidates.append(
            Candidate(
                "rollout_undo",
                {"namespace": ns, "name": workload, "kind": kind},
                "回滚到上一个可用版本，这是恢复服务最快的路径。",
                note="回滚后请修复镜像 tag / 仓库凭据，再重新发布。",
            )
        )
        return diag

    def _sig_pending(
        self, diag: Diagnosis, pods: list[PodInfo], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        diag.matched_signature = "pending_unschedulable"
        diag.confidence = "medium"
        pending = [p for p in pods if p.phase == "Pending"]
        diag.conclusion = (
            f"有 {len(pending)} 个 Pod 处于 **Pending**，无法调度到节点。"
            f"常见根因：节点资源不足、nodeSelector/亲和性不匹配、污点未容忍。"
        )
        for p in pending[:3]:
            diag.evidence.append(
                Evidence("state", f"Pod/{p.name}", f"{p.reason}: {p.message[:200]}", "kubectl describe pod")
            )
        try:
            for e in self.k8s.list_events(ns, involved_name=workload)[:5]:
                if e["reason"] in ("FailedScheduling", "NotTriggerScaleUp"):
                    diag.evidence.append(
                        Evidence("event", e["object"], f"{e['reason']}: {e['message'][:250]}", "events")
                    )
        except Exception:  # noqa: BLE001
            pass
        # 先排除"节点被人为 cordon"这一种：此时缩容治标不治本，
        # 正确动作是把节点恢复可调度。fuzz 生成用例暴露了这个盲区。
        cordoned = self._cordoned_nodes()
        if cordoned:
            for node in cordoned[:2]:
                diag.candidates.append(
                    Candidate(
                        "uncordon_node",
                        {"node": node},
                        f"节点 {node} 当前处于 cordon（不可调度）状态，"
                        f"这是 Pod 无法调度的直接原因——恢复其可调度性即可。",
                        note="若节点是**有意**隔离的（维护/排障），请勿执行；"
                             "先确认节点健康与维护窗口。",
                    )
                )
            diag.findings.append(
                f"💡 检测到 {len(cordoned)} 个节点处于不可调度状态，"
                f"这比缩容更可能是根因"
            )

        diag.candidates.append(
            Candidate(
                "scale_workload",
                {
                    "namespace": ns,
                    "name": workload,
                    "kind": kind,
                    "replicas": max(0, len(pods) - len(pending)),
                },
                "先缩容到可调度的副本数，缓解资源争抢。",
                note="⚠️ 只缓解不根治：需要扩容节点或修正调度约束。",
            )
        )
        return diag

    def _cordoned_nodes(self) -> list[str]:
        """返回当前不可调度（cordon）的节点名。取不到就返回空——不误报。"""
        try:
            return [n["name"] for n in self.k8s.list_nodes() if n.get("unschedulable")]
        except Exception:  # noqa: BLE001
            return []

    def _sig_crashloop(
        self, diag: Diagnosis, pods: list[PodInfo], workload: str, kind: str, ns: str
    ) -> Diagnosis:
        diag.matched_signature = "crashloop"
        diag.confidence = "medium"
        victim = next((p for p in pods if _has_reason([p], "CrashLoop")), pods[0])
        diag.conclusion = (
            f"容器处于 **CrashLoopBackOff**（重启 {victim.restarts} 次）。"
            f"需要结合上一次实例日志确定根因——常见为配置错误、依赖不可达、启动参数非法。"
        )
        diag.evidence.append(
            Evidence("state", f"Pod/{victim.name}", f"CrashLoopBackOff，重启 {victim.restarts} 次", "kubectl get pods")
        )
        # 关键：CrashLoopBackOff 必须看上一次实例的日志，当前实例往往没有日志
        try:
            logs = self.k8s.previous_pod_logs(ns, victim.name, tail=30)
            tail = "\n".join(logs.strip().splitlines()[-8:])
            if tail:
                diag.evidence.append(
                    Evidence("log", f"Pod/{victim.name} (previous)", tail, "kubectl logs --previous")
                )
                diag.findings.append("已读取上一次实例日志，见上方证据")
        except Exception as exc:  # noqa: BLE001
            diag.findings.append(f"读取上一次实例日志失败：{exc}")

        # --- 配置层优先：若连不上的地址与配置/实际端口对不上，根因是配置 ---
        # 必须排在通用"依赖不可达"检测**之前**——否则会被后者先认领，
        # 变成"根因在依赖侧、别动手"，而其实是本工作负载的 env 写错了。
        verdict = self._diagnose_connectivity(ns, kind, workload, tail)
        if verdict:
            return self._apply_connectivity(diag, verdict, workload, kind, ns, pods_healthy=False)

        # --- 假阳性拦截：根因在工作负载之外时，重启/回滚毫无作用 ---
        dep_evidence = detect_dependency_failure(tail)
        if dep_evidence:
            diag.confidence = "high"
            diag.conclusion = (
                f"容器反复重启，但日志显示根因是**下游依赖不可达**，"
                f"而不是本工作负载自身故障。\n\n"
                f"证据：{dep_evidence}\n\n"
                f"**对 api 做重启或回滚都不会有任何作用**——依赖恢复后容器会自行恢复正常。"
                f"应转交依赖方排查，或确认依赖地址/网络策略/凭据。"
            )
            diag.evidence.append(
                Evidence(
                    "log",
                    "依赖失败信号",
                    dep_evidence,
                    "依据日志判定根因为外部依赖",
                )
            )
            diag.findings.append(
                "⛔ 已拦截重启建议：根因在依赖侧，重启本工作负载属于无效动作"
            )
            # 关键：不给出任何变更动作
            diag.candidates = []
            return diag

        diag.candidates += [
            Candidate(
                "rollout_undo",
                {"namespace": ns, "name": workload, "kind": kind},
                "回滚到上一个稳定版本。若故障由最近一次发布引入，这是最有效的处置。",
            ),
            Candidate(
                "rollout_restart",
                {"namespace": ns, "name": workload, "kind": kind},
                "重启一次，排除偶发的启动竞态。",
                note="⚠️ 只缓解不根治：如果根因是配置错误，重启后会继续 CrashLoop。",
            ),
        ]
        return diag

    def _sig_notready(
        self,
        diag: Diagnosis,
        unhealthy: list[PodInfo],
        workload: str,
        kind: str,
        ns: str,
        signature: str = "not_ready",
    ) -> Diagnosis:
        # 签名必须保留调用方指定的值：probe_kill 与 not_ready 共用这套处置逻辑，
        # 但它们的**根因结论完全不同**，签名写死会把 probe_kill 抹成 not_ready。
        diag.matched_signature = signature
        diag.confidence = "medium"
        diag.conclusion = (
            f"有 {len(unhealthy)} 个 Pod 处于 Running 但未 Ready，"
            f"通常是**就绪探针失败**或依赖未就绪。"
        )
        for p in unhealthy[:3]:
            diag.evidence.append(
                Evidence("state", f"Pod/{p.name}", p.problem or "未就绪", "kubectl get pods")
            )

        # --- 配置层交叉比对：容器没起来，可能是连不上依赖（配置指错地址）---
        if unhealthy and diag.matched_signature != "probe_kill":
            try:
                vlogs = self.k8s.pod_logs(ns, unhealthy[0].name, tail=40)
            except Exception:  # noqa: BLE001
                vlogs = ""
            verdict = self._diagnose_connectivity(ns, kind, workload, vlogs)
            if verdict:
                return self._apply_connectivity(
                    diag, verdict, workload, kind, ns, pods_healthy=False
                )

        # --- 假阳性拦截：应用自己报告"已启动成功"，却未通过就绪探针 ---
        # 这通常意味着探针参数过严（initialDelay/failureThreshold/路径写错），
        # 重启工作负载只会白白抖动一次，属于误伤。
        started_ok: str | None = None
        if unhealthy:
            probe_victim = unhealthy[0]
            try:
                logs = self.k8s.pod_logs(ns, probe_victim.name, tail=30)
                started_ok = detect_started_ok(logs)
                if started_ok:
                    diag.evidence.append(
                        Evidence(
                            "log",
                            f"Pod/{probe_victim.name}",
                            started_ok,
                            "容器日志显示应用已启动成功",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                diag.findings.append(f"读取容器日志失败：{exc}")

        if diag.matched_signature == "probe_kill":
            diag.conclusion = (
                f"容器退出码 137，但**没有任何 OOMKilling 证据**，而事件里明确写着"
                f"存活探针失败后被 kubelet 重启。137 是 SIGKILL 的通用退出码，"
                f"**不能据此判定 OOMKilled**。\n\n"
                f"真实根因更可能是探针配置与启动耗时/健康检查路径不匹配。"
                f"调大内存不会有任何作用，应考虑回滚到上一个版本或修正探针参数。"
            )
            diag.confidence = "medium"
            diag.evidence.append(
                Evidence("event", f"Pod/{unhealthy[0].name}" if unhealthy else "Pod",
                         "Killing: Container failed liveness probe, will be restarted",
                         "events（探针误杀证据）")
            )
            diag.findings.append(
                "⛔ 已拦截「加大内存」建议：137 来自探针 SIGKILL，不是 OOMKilled"
            )

        # 探针误杀：重启改变不了探针配置，回滚到上一个版本才是有效动作
        if diag.matched_signature == "probe_kill" and not diag.candidates:
            diag.candidates.append(
                Candidate(
                    "rollout_undo",
                    {"namespace": ns, "name": workload, "kind": kind},
                    "回滚到上一个修订版本——若探针参数或健康检查路径是最近改坏的，"
                    "回滚是唯一能让服务恢复的有效动作。",
                    note="⚠️ 重启工作负载**无效**：探针配置不变，重启后会继续被误杀。",
                )
            )

        if started_ok:
            diag.confidence = "medium"
            diag.conclusion = (
                f"有 {len(unhealthy)} 个 Pod Running 但未 Ready，"
                f"而容器日志显示**应用自身已成功启动并开始服务**。\n\n"
                f"因此更可能是**就绪探针配置问题**（initialDelaySeconds 过小、"
                f"failureThreshold 过严、探测路径/端口写错），而不是应用故障。\n\n"
                f"**重启工作负载不会解决问题**，只会造成一次无谓抖动。"
            )
            diag.findings.append(
                "⛔ 已拦截重启建议：日志显示应用正常，疑似探针配置问题，需人工核对探针参数"
            )
            diag.candidates = []
            return diag

        diag.candidates.append(
            Candidate(
                "rollout_restart",
                {"namespace": ns, "name": workload, "kind": kind},
                "滚动重启，重新执行探针检查。",
                note="如果探针阈值配置过严，重启无法解决，需要调整探针参数。",
            )
        )
        return diag


def _double_memory(limit: str) -> str:
    """把 '512Mi' 粗略翻倍为 '1Gi'。仅用于生成建议值，实际仍需人工确认。"""
    limit = (limit or "").strip()
    for unit, factor in (("Gi", 1024), ("Mi", 1)):
        if limit.endswith(unit):
            try:
                val = float(limit[: -len(unit)])
            except ValueError:
                return "1Gi"
            doubled_mi = val * factor * 2
            if doubled_mi >= 1024:
                return f"{int(doubled_mi / 1024)}Gi"
            return f"{int(doubled_mi)}Mi"
    return "1Gi"


# ---------------------------------------------------------------------------
# LLM 规划器（可插拔）
# ---------------------------------------------------------------------------

# 故障特征分类taxonomy —— 与规则引擎的 matched_signature 取值保持一致，
# 这样两套规划器才能在同一把尺子上比较。
SIGNATURES = (
    "oom_killed",
    "crashloop",
    "image_pull",
    "pending_unschedulable",
    "not_ready",
    "no_pods",
    "healthy",
    "node_failure",       # 节点级故障（规则引擎尚不支持）
    "multi_root_cause",   # 多根因叠加（规则引擎尚不支持）
    "unknown",
)

DEFAULT_LLM_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-flash"

SYSTEM_PROMPT = """你是一个 Kubernetes 故障诊断助手。

你会收到一组**从集群采集的真实事实**（Pod 状态、事件、上一次实例日志等）。
你的任务是判断根因，并给出候选处置动作。

## 铁律

1. **只能使用清单中列出的动作**。清单之外的动作一律不许出现。
2. **禁止编造事实中不存在的资源名、字段或数值。** 信息不足时在 conclusion 里说明，不要猜。
3. **只在动作真能解决根因时才提议它。**

## 决策规则（请严格按此判断，既不要鲁莽，也不要无谓地不作为）

**应当给出候选动作**——当根因明确，且存在低风险的常规处置时：
- 容器 OOMKilled / CrashLoopBackOff → `rollout_restart` 或 `patch_resources`（若可回滚还有 `rollout_undo`）
- 镜像拉取失败 / 配置错误导致的启动失败 → `rollout_undo`
- 调度失败（资源不足、选择器不匹配、配额超限）→ `scale_workload`
- 就绪探针失败且**日志显示应用正常启动** → 这种情况下**不要**提议重启（属探针配置问题）
- 节点 Unknown / NotReady → `cordon_node` 隔离，**不要**重启工作负载

**应当返回空 candidates**——只在以下三种情况：
1. **根因在工作负载之外**：日志显示连不上外部依赖（connection refused / no such host /
   timeout 等）。此时重启或回滚本工作负载毫无作用，必须说明需转交依赖方。
2. **证据确实不足**：你已获取的证据无法支撑任何根因判断。请在 conclusion 中明确说明
   还缺什么信息，以及建议人工查看什么。
3. **服务本身健康**：没有任何异常，不需要变更。

**重要**：不要因为"不是 100% 确定"就选择不作为。把握不足时应降低 `confidence`
（high/medium/low）并说明假设，而不是放弃给建议。**只有在情况 1 或 2 时才可以不提议动作。**

## 可选动作清单

{tools}

## 输出格式

严格输出 JSON，不要输出任何其他文字：

{{
  "signature": "故障特征分类，必须严格从以下取值中选择其一：{signatures}",
  "conclusion": "根因判断，中文，2-4 句；若无法判断请说明还缺什么信息",
  "confidence": "high | medium | low",
  "evidence": [
    {{"kind": "state|log|event|config|metric", "ref": "资源引用", "detail": "证据内容"}}
  ],
  "candidates": [
    {{"tool": "动作名", "params": {{}}, "rationale": "为什么", "note": "局限或风险"}}
  ]
}}
"""


class LLMPlanner:
    """OpenAI 兼容接口的规划器（默认对接 DeepSeek）。

    安全边界说明（重要）
    --------------------
    LLM 在这里**不是安全边界**。它产出的只有 ``Diagnosis`` + ``Candidate``，
    必须依次经过：工具名白名单校验 → 参数校验 → 影响面分析 → 服务端 dry-run
    → 熔断规则 → 人工审批，才可能被执行。

    因此即使模型产生幻觉、编造出不存在的工具或危险动作，也无路可走。
    本类中的 ``_validate_candidates`` 是这条防线中最靠前的一道：
    清单外的工具会被**直接丢弃**，而不是交给下游去拒绝。

    环境变量
    --------
    - ``OMAGENT_LLM_API_KEY`` / ``DEEPSEEK_API_KEY``
    - ``OMAGENT_LLM_BASE_URL``（默认 https://api.deepseek.com/v1）
    - ``OMAGENT_LLM_MODEL``（默认 deepseek-flash）
    - ``OMAGENT_LLM_INCLUDE_LOGS``（默认 1；设为 0 则不发送日志，见下方隐私说明）
    """

    name = "llm"

    def __init__(
        self,
        k8s: K8sClient,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        include_logs: bool | None = None,
    ):
        self.k8s = k8s
        self.base_url = (
            base_url or os.environ.get("OMAGENT_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.environ.get("OMAGENT_LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or ""
        )
        self.model = model or os.environ.get("OMAGENT_LLM_MODEL") or DEFAULT_LLM_MODEL
        self.timeout = timeout
        if include_logs is None:
            include_logs = os.environ.get("OMAGENT_LLM_INCLUDE_LOGS", "1") != "0"
        self.include_logs = include_logs
        self.fallback = RuleBasedPlanner(k8s)

        # 成本核算（PRD 13.4）
        self.last_usage: dict[str, Any] = {}
        self.total_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.calls = 0
        self.dropped_tools: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    # ------------------------------------------------------------------ 主入口

    def diagnose(
        self,
        namespace: str,
        workload: str,
        kind: str = "deployment",
        extra_context: str | None = None,
        final_note: str | None = None,
    ) -> Diagnosis:
        if not self.available:
            # 降级：LLM 不可用不是故障，规则引擎顶上（PRD 第 10 节问题 9）
            diag = self.fallback.diagnose(namespace, workload, kind)
            diag.findings.append("LLM 规划器不可用（缺少 API Key 或 base_url），已降级到规则引擎")
            return diag

        facts = self._gather_facts(namespace, workload, kind)
        last_exc: Exception | None = None
        for attempt in (1, 2):  # 瞬时故障重试一次，避免直接降级
            try:
                payload = self._call_model(facts, extra_context, final_note)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 2:
                    diag = self.fallback.diagnose(namespace, workload, kind)
                    diag.findings.append(
                        f"LLM 调用失败（{exc}），已降级到规则引擎"
                    )
                    diag.findings.append(
                        "注：降级路径保证 Agent 在模型不可用时仍可用（PRD 第 10 节问题 9）"
                    )
                    return diag
        else:  # pragma: no cover - 循环必然 break 或 return
            raise RuntimeError("unreachable")

        return self._to_diagnosis(payload, namespace, workload, kind)

    # -------------------------------------------------------------- 事实采集

    def _gather_facts(self, namespace: str, workload: str, kind: str) -> dict[str, Any]:
        facts: dict[str, Any] = {"namespace": namespace, "workload": workload, "kind": kind}
        try:
            wl = self.k8s.read_workload(namespace, kind, workload)
            facts["desired_replicas"] = int(wl.spec.replicas or 0)
            tmpl = getattr(wl.spec, "template", None)
            if tmpl is not None:
                facts["pod_labels"] = dict(tmpl.metadata.labels or {})
                containers = []
                for c in tmpl.spec.containers or []:
                    res = c.resources
                    containers.append({
                        "name": c.name,
                        "image": c.image,
                        "limits": dict(res.limits or {}) if res and res.limits else {},
                        "requests": dict(res.requests or {}) if res and res.requests else {},
                    })
                facts["containers"] = containers
                facts["node_selector"] = dict(tmpl.spec.node_selector or {})
            # 历史版本数，判断"是否可以回滚"
            try:
                selector = (wl.spec.selector.match_labels or {}) if wl.spec.selector else {}
                sel = ",".join(f"{k}={v}" for k, v in selector.items())
                rss = self.k8s.apps.list_namespaced_replica_set(
                    namespace, label_selector=sel
                ).items
                facts["replicaset_count"] = len(rss)
                facts["rollback_available"] = len(rss) >= 2
            except Exception:  # noqa: BLE001
                facts["rollback_available"] = "unknown"
        except Exception as exc:  # noqa: BLE001
            facts["workload_error"] = str(exc)

        try:
            selector = facts.get("pod_labels") or {}
            sel = ",".join(f"{k}={v}" for k, v in selector.items())
            pods = self.k8s.list_pods(namespace, label_selector=sel)
            facts["pods"] = [
                {
                    "name": p.name,
                    "phase": p.phase,
                    "ready": p.ready,
                    "restarts": p.restarts,
                    "reason": p.reason,
                    "message": (p.message or "")[:200],
                    "last_exit_code": p.last_exit_code,
                    "memory_limit": p.memory_limit,
                }
                for p in pods
            ]
            # 上一次实例日志：区分"应用故障"与"依赖故障"的关键证据。
            # 隐私权衡：默认发送最后若干行，可用 OMAGENT_LLM_INCLUDE_LOGS=0 关闭。
            if self.include_logs:
                for p in pods:
                    if not p.healthy and p.restarts > 0:
                        logs = self.k8s.previous_pod_logs(namespace, p.name, tail=25)
                        if logs and not logs.startswith("<"):
                            facts["previous_log_tail"] = "\n".join(
                                logs.strip().splitlines()[-15:]
                            )[:2000]
                        break
        except Exception as exc:  # noqa: BLE001
            facts["pods_error"] = str(exc)

        try:
            facts["events"] = self.k8s.list_events(namespace, involved_name=workload)[:12]
        except Exception as exc:  # noqa: BLE001
            facts["events_error"] = str(exc)

        try:
            facts["pdb"] = self.k8s.list_pdb(namespace) or []
        except Exception:  # noqa: BLE001
            facts["pdb"] = []

        return facts

    # -------------------------------------------------------------- 模型调用

    def _tool_catalog(self) -> str:
        from .agent import TOOLS
        from .policy import Policy

        policy = Policy()
        lines = []
        for name, spec in sorted(TOOLS.items()):
            tier = policy.actions[name].tier if name in policy.actions else Tier.T3
            if tier.forbidden:
                continue  # 禁止动作不暴露给模型，减少被诱导的机会
            lines.append(f"- {name}（{tier.value} {tier.label}）：{spec.description}")
        # 同时给出参数要求
        params_lines = [
            f"  - {name} 参数: {spec.params or '无'}"
            for name, spec in sorted(TOOLS.items())
            if name in policy.actions and not policy.actions[name].tier.forbidden
        ]
        return "\n".join(lines) + "\n\n参数说明：\n" + "\n".join(params_lines)

    def _call_model(
        self,
        facts: dict[str, Any],
        extra_context: str | None = None,
        final_note: str | None = None,
    ) -> dict[str, Any]:
        prompt = (
            SYSTEM_PROMPT.format(tools=self._tool_catalog(), signatures=" | ".join(SIGNATURES))
            + "\n\n## 集群事实\n\n```json\n"
            + json.dumps(facts, ensure_ascii=False, default=str, indent=2)[:14000]
            + "\n```\n"
        )
        if extra_context:
            prompt += (
                "\n\n## 你此前要求的取证结果\n\n"
                "以下证据你已经拿到，**无需重复获取**。"
                "如果这些证据已经足以判断根因，请**直接给出候选动作**；"
                "只有在证据确实不足、或根因在工作负载之外时，才返回空 candidates 并说明原因。\n\n"
                + extra_context[:12000]
                + "\n"
            )
        if final_note:
            prompt += f"\n\n## 重要\n\n{final_note}\n"
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你是严谨的 Kubernetes 故障诊断助手，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                # 推理型模型会消耗 reasoning token；上限给足，否则 content 可能为空
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        # urllib 默认读取 http_proxy/https_proxy 环境变量；显式再兜一层
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        if proxy:
            handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()

        with opener.open(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        usage = data.get("usage") or {}
        self.last_usage = usage
        self.calls += 1
        for k in self.total_usage:
            self.total_usage[k] += int(usage.get(k, 0) or 0)

        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason", "?")

        if not content.strip():
            # content 为空最常见的原因是推理 token 吃满了配额。
            # 明确报出来，而不是抛一个含义模糊的 JSON 解析错误。
            raise RuntimeError(
                f"模型返回空内容（finish_reason={finish}, usage={usage}）。"
                f"通常意味着 max_tokens 被推理过程耗尽。"
            )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 容错：有时模型会在 JSON 外包一层 markdown 代码块
            stripped = content.strip()
            for fence in ("```json", "```"):
                stripped = stripped.removeprefix(fence)
            stripped = stripped.removesuffix("```").strip()
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"模型输出不是合法 JSON（finish_reason={finish}）：{content[:200]!r}"
                ) from exc

    # -------------------------------------------------------------- 结果转换

    def _validate_candidates(self, raw: list[dict[str, Any]]) -> tuple[list[Candidate], list[str]]:
        """白名单 + 参数校验。清单外的工具直接丢弃。

        这是"模型不能自己发明动作"的最靠前一道防线。
        """
        from .agent import TOOLS
        from .policy import Policy

        policy = Policy()
        kept: list[Candidate] = []
        dropped: list[str] = []

        for item in raw or []:
            tool = (item or {}).get("tool")
            if not tool:
                continue
            if tool not in TOOLS:
                dropped.append(f"{tool}（未登记的动作，已丢弃）")
                self.dropped_tools.append(tool)
                continue
            rule = policy.actions.get(tool)
            if rule is None or rule.tier.forbidden:
                dropped.append(f"{tool}（T3 禁止动作，已丢弃）")
                self.dropped_tools.append(tool)
                continue
            params = item.get("params") or {}
            if not isinstance(params, dict):
                dropped.append(f"{tool}（params 不是对象，已丢弃）")
                continue
            missing = [
                k
                for k, v in (TOOLS[tool].params or {}).items()
                if not str(v).endswith("?") and k not in params
            ]
            if missing:
                dropped.append(f"{tool}（缺少必需参数 {missing}，已丢弃）")
                continue
            kept.append(
                Candidate(
                    tool=tool,
                    params=params,
                    rationale=str(item.get("rationale", ""))[:500],
                    note=str(item.get("note", ""))[:300],
                )
            )
        return kept, dropped

    def _to_diagnosis(
        self, payload: dict[str, Any], namespace: str, workload: str, kind: str
    ) -> Diagnosis:
        diag = Diagnosis(
            namespace=namespace,
            workload=workload,
            kind=kind,
            conclusion=str(payload.get("conclusion", "")).strip(),
            confidence=str(payload.get("confidence", "medium")).strip().lower(),
            matched_signature="unknown",
        )
        raw_sig = str(payload.get("signature", "")).strip().lower()
        if raw_sig in SIGNATURES:
            diag.matched_signature = raw_sig
        else:
            diag.findings.append(f"LLM 返回了未知的 signature={raw_sig!r}，已记为 unknown")
        if diag.confidence not in {"high", "medium", "low"}:
            diag.confidence = "medium"

        for e in payload.get("evidence") or []:
            if not isinstance(e, dict):
                continue
            diag.evidence.append(
                Evidence(
                    kind=str(e.get("kind", "state")),
                    ref=str(e.get("ref", "")),
                    detail=str(e.get("detail", ""))[:400],
                    source=f"llm:{self.model}",
                )
            )

        kept, dropped = self._validate_candidates(payload.get("candidates") or [])
        diag.candidates = kept
        if dropped:
            diag.findings.append("LLM 产出的以下动作已在校验阶段丢弃：" + "；".join(dropped))
        if not diag.evidence:
            diag.findings.append("⚠️ LLM 未给出任何证据，结论可信度低")
        return diag


# ---------------------------------------------------------------------------
# 规划器工厂
# ---------------------------------------------------------------------------


def build_planner(k8s: K8sClient, prefer_llm: bool = False) -> Planner:
    """按可用性选择规划器。LLM 不可用时静默降级到规则引擎。"""
    if prefer_llm:
        llm = LLMPlanner(k8s)
        if llm.available:
            return llm
    return RuleBasedPlanner(k8s)
