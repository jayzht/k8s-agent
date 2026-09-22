"""O&M Agent 核心：诊断 → 方案 → 审批门禁 → 执行 → 审计。

本模块是整个产品安全性的落点。三条结构性不变量：

1. **工具级固定切分**：每个工具在 ``TOOLS`` 里声明 ``mutating``，启动时与
   config/policy.yaml 交叉校验，不一致直接拒绝启动。规划器无法改变它。
2. **propose 永不写**：``OpsAgent.propose()`` 只做评估（影响面 + 服务端 dry-run +
   熔断），绝不产生副作用。写操作只能经由 ``execute()``。
3. **execute 必须要批准**：``execute()`` 拒绝执行任何 mutating 方案，除非传入
   一个 ``approved=True`` 且 ``proposal_id`` 与方案严格匹配的 ``Decision``。

这三条合起来的效果是：**即使规划器是 LLM 并且产生了幻觉，它也没有路径执行
一个未经人工批准的写操作。**
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog, new_trace_id
from .impact import analyse_node_impact, analyse_workload_impact
from .k8s import ActionRejected, K8sClient, K8sUnavailable
from .models import (
    Decision,
    Evidence,
    ExecutionResult,
    Impact,
    Proposal,
    Target,
    Tier,
)
from .policy import Policy

# ---------------------------------------------------------------------------
# 工具登记表
#
# mutating 是**工具定义本身的属性**——这是"固定切分"的代码侧声明。
# 它与 config/policy.yaml 的 actions 段必须一致，否则启动失败。
# ---------------------------------------------------------------------------
TOOL_TARGET = "workload"  # 其余取值: pod | node | hpa


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mutating: bool
    description: str
    target: str  # workload | pod | node | hpa
    params: dict[str, str]


TOOLS: dict[str, ToolSpec] = {
    # --- T0 只读：自动放行，且**真正可执行**（见 run_diagnostic）---
    "get_pods": ToolSpec(
        "get_pods", False, "列出命名空间下的 Pod 及状态", "readonly",
        {"namespace": "str", "label_selector": "str?"},
    ),
    "get_events": ToolSpec(
        "get_events", False, "读取命名空间或指定对象的事件", "readonly",
        {"namespace": "str", "name": "str?"},
    ),
    "get_logs": ToolSpec(
        "get_logs", False, "读取 Pod 日志（可指定上一次实例）", "readonly",
        {"namespace": "str", "pod": "str", "container": "str?",
         "tail_lines": "int?", "previous": "bool?"},
    ),
    "get_pdb": ToolSpec(
        "get_pdb", False, "读取 PodDisruptionBudget", "readonly", {"namespace": "str"}
    ),
    "get_workload": ToolSpec(
        "get_workload", False, "读取工作负载规格（副本/镜像/资源/调度约束）", "readonly",
        {"namespace": "str", "name": "str", "kind": "str?"},
    ),
    "get_nodes": ToolSpec(
        "get_nodes", False, "列出节点状态", "readonly", {}
    ),
    "get_endpoints": ToolSpec(
        "get_endpoints", False, "查询 Service 的后端端点（判断有无可用后端）", "readonly",
        {"namespace": "str", "name": "str?"},
    ),
    "get_services": ToolSpec(
        "get_services", False, "列出 Service 及其实端口（与配置交叉比对用）", "readonly",
        {"namespace": "str"},
    ),
    "get_configmap": ToolSpec(
        "get_configmap", False, "读取 ConfigMap 内容（只读；永不接触 Secret）", "readonly",
        {"namespace": "str", "name": "str"},
    ),
    # --- T1 低危写：一键确认 ---
    "rollout_restart": ToolSpec(
        # kind 可省略，默认 deployment（见 _build_target / apply_mutation）
        "rollout_restart", True, "滚动重启工作负载", "workload",
        {"namespace": "str", "name": "str", "kind": "str?"}
    ),
    "rollout_undo": ToolSpec("rollout_undo", True, "回滚到上一个版本", "workload",
        {"namespace": "str", "name": "str"}),
    "scale_workload": ToolSpec(
        "scale_workload", True, "调整副本数", "workload",
        {"namespace": "str", "name": "str", "kind": "str?", "replicas": "int"}
    ),
    "delete_pod": ToolSpec(
        "delete_pod", True, "删除单个 Pod（触发重建）", "pod",
        {"namespace": "str", "pod": "str"}
    ),
    "cordon_node": ToolSpec(
        "cordon_node", True, "标记节点不可调度", "node", {"node": "str"}
    ),
    "uncordon_node": ToolSpec(
        "uncordon_node", True, "恢复节点可调度（cordon 的回滚动作）", "node", {"node": "str"}
    ),
    # --- T2 中危写：强确认 ---
    "drain_node": ToolSpec(
        "drain_node", True, "驱逐节点上的 Pod（完整 drain 语义）", "node",
        {"node": "str", "ignore_daemonsets": "bool?", "delete_emptydir_data": "bool?"},
    ),
    "patch_resources": ToolSpec(
        "patch_resources", True, "调整资源 requests/limits", "workload",
        {"namespace": "str", "name": "str", "container": "str",
         "memory_limit": "str?", "cpu_limit": "str?"},
    ),
    "rollback_configmap": ToolSpec(
        "rollback_configmap", True, "把 ConfigMap 回滚到历史版本（配置层写动作）", "configmap",
        {"namespace": "str", "name": "str", "data": "object"},
    ),
    "patch_hpa": ToolSpec(
        "patch_hpa", True, "调整 HPA 副本区间", "hpa",
        {"namespace": "str", "hpa": "str",
         "min_replicas": "int?", "max_replicas": "int?"},
    ),
    # --- T3 禁止：登记在册以实现纵深防御 ---
    # 这些动作永远不会被执行（k8s.apply_mutation 中没有实现），
    # 登记的目的是让门禁能识别它们并给出明确的拒绝原因，而不是"未知动作"。
    "delete_workload": ToolSpec(
        "delete_workload", True, "删除工作负载（禁止）", "workload",
        {"namespace": "str", "name": "str", "kind": "str?"}
    ),
    "delete_namespace": ToolSpec(
        "delete_namespace", True, "删除命名空间（禁止）", "namespace", {}
    ),
    "delete_pvc": ToolSpec("delete_pvc", True, "删除 PVC（禁止）", "namespace", {}),
    "modify_rbac": ToolSpec("modify_rbac", True, "修改 RBAC（禁止）", "namespace", {}),
    "read_secret": ToolSpec("read_secret", True, "读取 Secret（禁止）", "namespace", {}),
}


class GateViolation(RuntimeError):
    """试图绕过审批门禁。这是必须留痕的安全事件。"""


class Refusal:
    """一次被拒绝的请求。拒绝也是一种产品输出（剧本 2）。"""

    def __init__(self, rule_id: str, desc: str, hint: str, request: str = ""):
        self.rule_id = rule_id
        self.desc = desc
        self.hint = hint
        self.request = request

    def render(self) -> str:
        return (
            f"⛔ 拒绝执行：{self.desc}\n"
            f"   规则：{self.rule_id}\n"
            f"   建议：{self.hint}"
        )


class OpsAgent:
    """面向 Kubernetes 的 L1 档位运维 Agent。"""

    def __init__(
        self,
        k8s: K8sClient,
        policy: Policy,
        audit: AuditLog,
        operator: str = "unknown",
        dry_run_only: bool = False,
    ):
        self.k8s = k8s
        self.policy = policy
        self.audit = audit
        self.operator = operator
        self.dry_run_only = dry_run_only
        self._verify_tool_consistency()

    def _verify_tool_consistency(self) -> None:
        """启动期完整性校验：代码声明与策略文件必须一致（fail closed）。"""
        for spec in TOOLS.values():
            self.policy.assert_consistent(spec.name, spec.mutating)

    # ------------------------------------------------------------------ 提案

    def propose(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        rationale: str = "",
        note: str = "",
        evidence: list[Evidence] | None = None,
        trace_id: str | None = None,
    ) -> Proposal:
        """构建并完整评估一个方案。**绝不执行写操作。**

        流程：登记校验 → 影响面 → 服务端 dry-run → 熔断评估 → 审计落库
        """
        trace_id = trace_id or new_trace_id()
        rule = self.policy.rule(tool)  # 未登记动作在这里抛错
        spec = TOOLS.get(tool)
        if spec is None:
            raise GateViolation(f"工具 {tool!r} 未在代码侧登记")

        target = self._build_target(spec, params)

        prop = Proposal(
            tool=tool,
            params=dict(params),
            tier=rule.tier,
            target=target,
            rationale=rationale,
            note=note,
            evidence=list(evidence or []),
            rollback=rule.rollback,
            rollback_eta=rule.rollback_eta,
        )

        # 影响面（只读操作也计算，用于展示；写操作必须计算）
        prop.impact = self._analyse(spec, params, target)

        # T3 直接短路：不进 dry-run，不生成可执行方案
        if rule.tier.forbidden:
            self.policy.evaluate(prop)
            self.audit.log_proposal(trace_id, self.operator, prop)
            return prop

        # 服务端 dry-run（写操作强制）
        if rule.mutating:
            prop.dry_run_ok, prop.dry_run_output = self._dry_run(prop)

        # 熔断评估
        labels = self._target_labels(spec, params, target)
        self.policy.evaluate(prop, labels=labels)

        self.audit.log_proposal(trace_id, self.operator, prop)
        return prop

    def _build_target(self, spec: ToolSpec, params: dict[str, Any]) -> Target:
        ns = params.get("namespace", "")
        if spec.target == "configmap":
            return Target(kind="ConfigMap", namespace=ns, name=params.get("name", ""))
        if spec.target == "readonly":
            return Target(
                kind="ReadOnly", namespace=ns,
                name=params.get("name", params.get("pod", "")),
            )
        if spec.target == "namespace":
            return Target(kind="Namespace", namespace="", name=ns or params.get("name", ""))
        if spec.target == "node":
            return Target(kind="Node", namespace="", name=params["node"])
        if spec.target == "pod":
            return Target(kind="Pod", namespace=ns, name=params.get("pod", params.get("name", "")))
        if spec.target == "hpa":
            return Target(kind="HorizontalPodAutoscaler", namespace=ns, name=params["hpa"])
        return Target(
            kind=params.get("kind", "Deployment").capitalize(),
            namespace=ns,
            name=params.get("name", ""),
        )

    def _analyse(self, spec: ToolSpec, params: dict[str, Any], target: Target):
        try:
            if spec.target in ("readonly", "configmap"):
                return Impact()
            if spec.target == "node":
                return analyse_node_impact(self.k8s, params["node"])
            if spec.target == "hpa":
                return analyse_workload_impact(
                    self.k8s, target.namespace, "Deployment", params.get("name", target.name)
                )
            return analyse_workload_impact(
                self.k8s, target.namespace, params.get("kind", "Deployment"), target.name
            )
        except Exception as exc:  # noqa: BLE001
            from .models import Impact

            imp = Impact()
            imp.notes.append(f"影响面分析失败: {exc}")
            return imp

    def _target_labels(
        self, spec: ToolSpec, params: dict[str, Any], target: Target
    ) -> dict[str, str]:
        """取目标资源标签，供保护标签规则使用。"""
        if spec.target != "workload":
            return {}
        try:
            wl = self.k8s.read_workload(target.namespace, params.get("kind", "Deployment"), target.name)
            return dict(wl.metadata.labels or {})
        except Exception:  # noqa: BLE001
            return {}

    def _dry_run(self, prop: Proposal) -> tuple[bool, str]:
        """服务端 dry-run（dry_run='All'）。不产生任何副作用。"""
        payload = dict(prop.params)
        payload.setdefault("namespace", prop.target.namespace)
        payload.setdefault("name", prop.target.name)
        try:
            out = self.k8s.apply_mutation(prop.tool, payload, dry_run=True)
            return True, out
        except ActionRejected as exc:
            return False, str(exc)
        except K8sUnavailable as exc:
            return False, f"集群不可达，无法完成 dry-run: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"dry-run 异常: {exc}"

    # -------------------------------------------------------------- 只读诊断

    def run_diagnostic(self, tool: str, params: dict[str, Any]) -> str:
        """执行一次只读诊断（T0，自动放行，不经审批门禁）。

        **结构性约束**：本方法只接受非 mutating 工具。任何写操作走到这里都会
        抛 GateViolation，而不是被"顺手执行掉"。这保证了"取证"与"变更"在代码
        路径上就是分开的，不依赖调用方自觉。
        """
        spec = TOOLS.get(tool)
        if spec is None:
            raise GateViolation(f"工具 {tool!r} 未在代码侧登记")
        if spec.mutating:
            raise GateViolation(
                f"{tool!r} 会改变集群状态，不能经 run_diagnostic 执行——"
                f"必须先 propose() 并通过人工审批后 execute()"
            )
        rule = self.policy.rule(tool)  # 未登记动作在这里抛错
        if rule.tier.forbidden:
            raise GateViolation(f"{tool!r} 属于 T3 禁止动作")
        return self.k8s.read_only_query(tool, params)

    # ------------------------------------------------------------------ 执行

    def execute(
        self,
        prop: Proposal,
        decision: Decision | None = None,
        *,
        dry_run: bool = False,
        trace_id: str | None = None,
    ) -> ExecutionResult:
        """执行方案。**没有批准就拒绝。**

        门禁顺序：禁止动作 → 熔断 block → 审批匹配 → dry-run 复核 → 执行。
        """
        trace_id = trace_id or new_trace_id()
        started = time.time()

        def _result(status: str, output: str = "", error: str = "") -> ExecutionResult:
            res = ExecutionResult(
                proposal_id=prop.proposal_id,
                status=status,
                output=output,
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )
            self.audit.log_execution(trace_id, res)
            return res

        # --- 门禁 1：T3 禁止动作，任何人都批不了 ---
        if prop.effective_tier.forbidden:
            self.audit.append(
                "gate_violation",
                {
                    "trace_id": trace_id,
                    "operator": self.operator,
                    "reason": "forbidden_tier",
                    "tool": prop.tool,
                },
            )
            return _result("refused", error=f"{prop.tool} 属于 T3 禁止动作，拒绝执行")

        # --- 门禁 2：熔断 block ---
        if prop.blocked:
            detail = "; ".join(b.detail for b in prop.breaches if b.severity == "block")
            self.audit.append(
                "gate_violation",
                {
                    "trace_id": trace_id,
                    "operator": self.operator,
                    "reason": "circuit_breaker",
                    "tool": prop.tool,
                    "detail": detail,
                },
            )
            return _result("refused", error=f"熔断规则拦截：{detail}")

        # --- 门禁 3：审批必须匹配 ---
        if prop.requires_approval or not dry_run:
            if decision is None:
                self.audit.append(
                    "gate_violation",
                    {
                        "trace_id": trace_id,
                        "operator": self.operator,
                        "reason": "missing_approval",
                        "tool": prop.tool,
                        "proposal_id": prop.proposal_id,
                    },
                )
                return _result(
                    "refused",
                    error=f"{prop.effective_tier.value} 动作需要人工批准，但未提供 Decision",
                )
            if decision.proposal_id != prop.proposal_id:
                self.audit.append(
                    "gate_violation",
                    {
                        "trace_id": trace_id,
                        "operator": self.operator,
                        "reason": "approval_mismatch",
                        "tool": prop.tool,
                        "expected": prop.proposal_id,
                        "got": decision.proposal_id,
                    },
                )
                return _result("refused", error="批准凭证与方案不匹配（可能被调包）")
            if not decision.approved:
                return _result("cancelled", error=f"操作人拒绝：{decision.reason or '未填理由'}")

        if self.dry_run_only and prop.tier.requires_approval:
            return _result("refused", error="当前处于只读模式（dry_run_only），不执行写操作")

        # --- 执行 ---
        payload = dict(prop.params)
        payload.setdefault("namespace", prop.target.namespace)
        payload.setdefault("name", prop.target.name)
        try:
            out = self.k8s.apply_mutation(prop.tool, payload, dry_run=dry_run)
            if prop.tier.requires_approval and not dry_run:
                key = f"{prop.target.namespace}/{prop.target.kind}/{prop.target.name}"
                self.policy.record_change(key)
            return _result("success", output=out)
        except ActionRejected as exc:
            return _result("failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return _result("failed", error=f"执行异常: {exc}")

    # ------------------------------------------------------------------ 拒绝

    def refuse(self, request: str, rule_id: str) -> Refusal:
        """拒绝一个越界请求，并按策略给出替代建议。拒绝必须留痕。"""
        spec = self.policy.forbidden_actions.get(rule_id)
        if spec is None:
            desc, hint = f"未知禁止项 {rule_id}", "请联系平台团队。"
        else:
            desc, hint = spec["desc"], spec["hint"]
        refusal = Refusal(rule_id, desc, hint, request=request)
        self.audit.log_refusal(
            new_trace_id(), self.operator, request, f"{rule_id}:{desc}", hint
        )
        return refusal

    # ------------------------------------------------------------------ 状态

    def cluster_status(self) -> tuple[bool, str]:
        return self.k8s.ping()

    def describe_self(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "dry_run_only": self.dry_run_only,
            "tools": {n: {"mutating": s.mutating, "target": s.target} for n, s in TOOLS.items()},
            "policy": self.policy.summary(),
            "audit": str(self.audit.path),
        }
