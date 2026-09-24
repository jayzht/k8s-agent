"""Agent 核心：只读自动执行 → 写操作必须人工批准。

整个文件只有两条执行路径，没有第三条：

    run_readonly(tool, params)          只读。自动执行，不需要任何人同意。
    execute_write(proposal, decision)   写入。没有匹配的批准凭证，执行不了。

想绕过它需要绕过代码，而不是绕过一段提示词。这是刻意的：
**模型可以说任何话，能做的事仍然只有这些。**

三个关键的"fail closed"决定：

1. ``is_mutating()`` 对未登记工具返回 True —— 没声明清楚的一律当写操作。
2. ``run_readonly()`` 主动拒绝写工具 —— 不是"调用方要小心"，是这里就过不去。
3. 参数校验按 schema 白名单裁剪 —— 多传的字段直接丢掉，而不是转发给 API Server。
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable

from .audit import AuditLog, new_trace_id
from .cases import OUTCOME_APPLIED, Case, CaseStore, describe, symptom_tokens
from .impact import analyse_node_impact, analyse_workload_impact
from .k8s import ActionRejected, K8sClient, K8sUnavailable
from .models import Decision, ExecutionResult, Impact, Proposal, Target
from .providers import ProviderRegistry
from .tools import (
    FORBIDDEN,
    RATIONALE_FIELD,
    TOOLS,
    ToolSpec,
    is_mutating,
    openai_tool_schema,
    ui_tool_list,
)

# ---------------------------------------------------------------------------
# 写操作的命名空间护栏
#
# 这是硬编码的常量，不是配置文件——它不该需要"上线后调参"，它就是"别在生产上
# 乱动"。想放开就改这一行，改的时候你会看见自己在做什么。
#
# 只读不受此限制：排查问题时需要看 kube-system。
# ---------------------------------------------------------------------------
WRITE_NAMESPACES: tuple[str, ...] = ("demo", "staging")


class GateViolation(RuntimeError):
    """试图绕过审批门禁。这是必须留痕的安全事件。"""


class OpsAgent:
    def __init__(
        self,
        k8s: K8sClient,
        audit: AuditLog,
        operator: str = "unknown",
        write_namespaces: tuple[str, ...] = WRITE_NAMESPACES,
        cases: CaseStore | None = None,
        providers: "ProviderRegistry | None" = None,
    ):
        self.k8s = k8s
        self.audit = audit
        self.operator = operator
        self.write_namespaces = tuple(write_namespaces)
        # 语义记忆。没传就是没有记忆——功能降级，但绝不假装记得什么。
        self.cases = cases
        # 外部只读数据源（Prometheus / Loki / ...）。没配就是空表，
        # 行为与"只有 K8s 工具"时完全一致。
        self.providers = providers if providers is not None else ProviderRegistry()
        # 同一时刻只允许一个写操作落地，避免两条线程同时改同一个工作负载
        self.write_lock = threading.Lock()

    # -------------------------------------------------------------- 工具清单

    def tool_schema(self) -> list[dict[str, Any]]:
        """发给模型的完整工具清单 = 内置 K8s 工具 + 已装配的 provider 工具。

        内置的读写切分是**静态**的（``tools.py``）：那是安全边界，不参与动态装配。
        provider 侧只能是只读的，由 ``ProviderRegistry`` 在构造时强制。
        """
        return openai_tool_schema() + self.providers.openai_schema()

    def ui_tools(self) -> list[dict[str, Any]]:
        """给前端「工具能力」面板用，形状与 ``tools.ui_tool_list()`` 一致。"""
        return ui_tool_list() + self.providers.ui_list()

    def all_tool_specs(self) -> dict[str, ToolSpec]:
        out: dict[str, ToolSpec] = dict(TOOLS)
        out.update({s.name: s for s in self.providers.specs()})
        return out

    # ---------------------------------------------------------------- 只读路径

    def run_readonly(self, tool: str, params: dict[str, Any]) -> str:
        """执行一次只读取证。自动放行，因为只读本来就不需要许可。

        **结构性约束**：这里主动拒绝写工具。不是因为调用方会犯错，而是因为
        "取证"和"变更"在代码上就该是两条路——只读循环里永远不可能漏进一个写操作。

        只读也要留痕。它不需要许可，但"Agent 到底看了什么"必须可回溯——
        否则出了事没人能复盘它的判断依据。
        """
        started = time.time()
        if tool in FORBIDDEN:
            raise GateViolation(f"{tool} 属于禁止动作：{FORBIDDEN[tool]}")
        # 内置 K8s 工具为主；不在内置表里的，看是不是已装配的 provider 工具。
        # 两边都没有 → 拒绝（fail closed）。
        spec = TOOLS.get(tool)
        from_provider = False
        if spec is None:
            spec = self.providers.spec(tool)
            from_provider = spec is not None
        if spec is None:
            raise GateViolation(f"工具 {tool!r} 未登记")
        if spec.mutating:
            raise GateViolation(
                f"{tool!r} 会改变集群状态，不能走只读路径——必须先提出方案并由人批准"
            )
        clean = self.validate_params(tool, params)
        try:
            # 案例检索不是 K8s 查询，走 agent 自己；但仍然是**只读**路径，
            # 所以它在工具表里就是只读的，不需要额外审批。
            if tool == "search_cases":
                out = self._search_cases(clean)
            elif from_provider:
                out = self.providers.call(tool, clean)
            else:
                out = self.k8s.read_only_query(tool, clean)
        except Exception as exc:
            self.audit.append("readonly_call", {
                "operator": self.operator, "tool": tool, "params": clean,
                "ok": False, "error": str(exc),
                "ms": int((time.time() - started) * 1000),
            })
            raise
        self.audit.append("readonly_call", {
            "operator": self.operator, "tool": tool, "params": clean, "ok": True,
            "ms": int((time.time() - started) * 1000),
        })
        return out

    # ---------------------------------------------------------------- 写入路径

    def propose(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        rationale: str = "",
        rollback: str = "",
        trace_id: str | None = None,
    ) -> Proposal:
        """构建并评估一个写方案。**绝不执行。**

        评估包括影响面分析和一次服务端 dry-run（dry_run=All，无副作用），
        目的是让审批卡片上有真东西可看，而不是一句"模型建议重启"。
        """
        trace_id = trace_id or new_trace_id()

        if tool in FORBIDDEN:
            raise GateViolation(f"{tool} 属于禁止动作，不会被生成方案：{FORBIDDEN[tool]}")
        spec = TOOLS.get(tool)
        if spec is None:
            raise GateViolation(f"工具 {tool!r} 未登记，拒绝生成方案")
        if not spec.mutating:
            raise GateViolation(
                f"{tool!r} 是只读工具，不需要审批——直接调用即可"
            )

        # 显式传进来的 rationale 与参数里的等价，两条调用路径行为一致
        payload = dict(params)
        if rationale:
            payload.setdefault(RATIONALE_FIELD, rationale)
        clean = self.validate_params(tool, payload)
        # rationale 是"给人看的理由"，不是发给 API Server 的字段——在构造方案前摘掉。
        # schema 里它是 required，所以无论是模型调用还是代码调用，都必须先回答"为什么"。
        stated = str(clean.pop(RATIONALE_FIELD, "") or "").strip() or rationale.strip()
        self._assert_writable_namespace(spec, clean)

        prop = Proposal(
            tool=tool,
            params=clean,
            target=self.target_of(spec, clean),
            rationale=stated or rationale or "（模型未说明理由）",
            rollback=rollback or self._default_rollback(tool),
        )
        prop.impact = self._analyse(spec, clean, prop.target)
        # 症状签名要在**提方案这一刻**算：执行成功之后症状就消失了，
        # 那时候再算就只能记下"一切正常"。
        prop.signature = self._signature(spec, clean, prop.target)
        prop.dry_run_ok, prop.dry_run_output = self._dry_run(prop)

        self.audit.log_proposal(trace_id, self.operator, prop)
        return prop

    def execute_write(
        self,
        prop: Proposal,
        decision: Decision | None = None,
        *,
        trace_id: str | None = None,
    ) -> ExecutionResult:
        """执行写方案。**门禁在这里，只有这一个入口。**

        顺序：禁止动作 → 命名空间护栏 → 批准凭证匹配 → dry-run 未通过则拒 → 执行。
        """
        trace_id = trace_id or new_trace_id()
        started = time.time()

        def result(status: str, output: str = "", error: str = "") -> ExecutionResult:
            res = ExecutionResult(
                proposal_id=prop.proposal_id,
                status=status,
                output=output,
                error=error,
                duration_ms=int((time.time() - started) * 1000),
            )
            self.audit.log_execution(trace_id, res)
            return res

        def violation(reason: str, **extra: Any) -> None:
            self.audit.append(
                "gate_violation",
                {"trace_id": trace_id, "operator": self.operator, "reason": reason,
                 "tool": prop.tool, "proposal_id": prop.proposal_id, **extra},
            )

        if prop.tool in FORBIDDEN:
            violation("forbidden_tool")
            return result("refused", error=f"{prop.tool} 属于禁止动作：{FORBIDDEN[prop.tool]}")

        if is_mutating(prop.tool) is False:
            violation("not_a_write")
            return result("refused", error=f"{prop.tool} 是只读工具，不该走写路径")

        spec = TOOLS.get(prop.tool)
        if spec is None:
            violation("unregistered_tool")
            return result("refused", error=f"{prop.tool} 未登记")

        try:
            self._assert_writable_namespace(spec, prop.params)
        except GateViolation as exc:
            violation("namespace_not_allowed", detail=str(exc))
            return result("refused", error=str(exc))

        # --- 门禁：必须有一个与方案严格绑定的批准凭证 ---
        if decision is None:
            violation("missing_approval")
            return result("refused", error="写操作需要人工批准，但没有收到批准凭证")
        if decision.proposal_id != prop.proposal_id:
            violation("approval_mismatch", expected=prop.proposal_id, got=decision.proposal_id)
            return result("refused", error="批准凭证与方案不匹配（方案可能被调包）")
        if not decision.approved:
            return result("cancelled", error=f"运维人员拒绝：{decision.reason or '未填理由'}")

        # dry-run 没过的东西，人工批准也不执行。
        # 因为"人点了同意"和"API Server 会不会接受"是两回事；
        # 让一个必然失败的操作打上"已批准"的标签，只会污染审计记录。
        if prop.dry_run_ok is False:
            violation("dry_run_failed")
            return result("refused", error=f"服务端 dry-run 未通过，不执行：{prop.dry_run_output}")

        # --- 门禁 4：执行前**重跑**一次 dry-run ---
        #
        # 卡片上的 dry-run 结果是 propose 那一刻的，而人可能几分钟后才点批准。
        # 这中间集群一定会变：副本被别处改过、对象被删了、配额被占满了。
        # 拿一个陈旧的结论去执行，正是运维里最危险的那个时间窗——
        # "检查的时候没事"不等于"执行的时候没事"。
        fresh_ok, fresh_out = self._dry_run(prop)
        if not fresh_ok:
            violation("dry_run_stale", detail=fresh_out)
            return result(
                "refused",
                error=f"批准后重新干跑未通过，已拒绝执行：{fresh_out}"
                      f"（方案基于生成时的集群状态，可能已过期，请重新诊断）",
            )
        if fresh_out != prop.dry_run_output:
            # 干跑结果变了，说明集群状态在这期间动过。不阻塞执行，但要留痕。
            self.audit.append(
                "dry_run_changed",
                {"trace_id": trace_id, "operator": self.operator, "tool": prop.tool,
                 "proposal_id": prop.proposal_id,
                 "before": prop.dry_run_output[:400], "after": fresh_out[:400]},
            )

        # --- 执行 ---
        with self.write_lock:
            try:
                out = self.k8s.apply_mutation(prop.tool, prop.params, dry_run=False)
                return result("success", output=out)
            except ActionRejected as exc:
                return result("failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                return result("failed", error=f"执行异常: {exc}")

    def dry_run(self, prop: Proposal) -> tuple[bool, str]:
        """只做服务端干跑，给"预览一下会发生什么"用。"""
        return self._dry_run(prop)

    # ------------------------------------------------------------ 参数与目标

    def validate_params(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """按 schema 校验并**裁剪**参数。

        多传的字段直接丢弃，而不是转发给 API Server——模型（或任何上游）能表达的
        东西，被限制在 schema 之内。校验失败抛出的消息是写给模型看的，
        它读到之后能自己改对参数重试。
        """
        spec = TOOLS[tool]
        schema = spec.schema
        props: dict[str, Any] = schema.get("properties", {})
        if not isinstance(params, dict):
            raise GateViolation(f"{tool}: 参数必须是对象，收到 {type(params).__name__}")

        clean: dict[str, Any] = {}
        for key, value in params.items():
            if key not in props:
                continue  # 白名单之外，丢弃
            if value is None or value == "":
                continue
            spec_kind = props[key].get("type")
            try:
                if spec_kind == "integer":
                    clean[key] = int(value)
                elif spec_kind == "boolean":
                    clean[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
                elif spec_kind == "object":
                    if not isinstance(value, dict):
                        raise ValueError("需要对象")
                    clean[key] = {str(k): str(v) for k, v in value.items()}
                else:
                    clean[key] = str(value)
            except (TypeError, ValueError) as exc:
                raise GateViolation(f"{tool}: 参数 {key}={value!r} 类型不对（{exc}）") from exc

        missing = [r for r in spec.required if r not in clean]
        if missing:
            raise GateViolation(
                f"{tool}: 缺少必需参数 {', '.join(missing)}。"
                f"该工具的完整参数说明：{props}"
            )
        return clean

    @staticmethod
    def target_of(spec: ToolSpec, params: dict[str, Any]) -> Target:
        """从参数推出"这个动作作用在谁身上"，供审批卡片展示。"""
        ns = params.get("namespace", "")
        if spec.name in ("cordon_node", "uncordon_node", "drain_node"):
            return Target(kind="Node", namespace="", name=params.get("node", ""))
        if spec.name == "patch_hpa":
            return Target(kind="HorizontalPodAutoscaler", namespace=ns, name=params.get("hpa", ""))
        if spec.name == "delete_pod":
            return Target(kind="Pod", namespace=ns, name=params.get("pod", ""))
        if spec.name == "rollback_configmap":
            return Target(kind="ConfigMap", namespace=ns, name=params.get("name", ""))
        return Target(
            kind=params.get("kind", "deployment").capitalize(),
            namespace=ns,
            name=params.get("name", ""),
        )

    def _assert_writable_namespace(self, spec: ToolSpec, params: dict[str, Any]) -> None:
        """节点级动作没有命名空间，靠集群本身的 RBAC 兜底；其余必须在允许列表内。"""
        if spec.name in ("cordon_node", "uncordon_node", "drain_node"):
            return
        ns = params.get("namespace", "")
        if ns not in self.write_namespaces:
            raise GateViolation(
                f"命名空间 {ns!r} 不在写操作允许列表内（当前允许：{', '.join(self.write_namespaces)}）"
            )

    @staticmethod
    def _default_rollback(tool: str) -> str:
        return {
            "rollout_restart": "无自动回滚；重启不改变规格，必要时可再次重启",
            "rollout_undo": "再执行一次 rollout_undo 会继续往前回滚一个版本",
            "scale_workload": "把 replicas 改回原值",
            "delete_pod": "无需回滚，控制器会自动重建",
            "patch_resources": "把 resources 改回原值（get_workload 里有原始值）",
            "patch_hpa": "把 min/maxReplicas 改回原值",
            "cordon_node": "uncordon_node 恢复调度",
            "uncordon_node": "cordon_node 撤销",
            "drain_node": "uncordon_node 恢复调度；已驱逐的 Pod 由控制器重新调度",
            "rollback_configmap": "把 data 再改回当前值",
        }.get(tool, "无自动回滚")

    # ---------------------------------------------------------------- 影响面

    def _analyse(self, spec: ToolSpec, params: dict[str, Any], target: Target) -> Impact:
        try:
            if spec.name in ("cordon_node", "uncordon_node", "drain_node"):
                return analyse_node_impact(self.k8s, params.get("node", ""))
            if spec.name == "rollback_configmap":
                return Impact()
            kind = params.get("kind", "deployment")
            name = params.get("name") or target.name
            # scale 类操作必须按**目标**副本数算影响面：否则 3→1 和 3→10
            # 会给出同一张卡片，而前者会把有冗余的服务变成单点。
            target_replicas = None
            if spec.name == "scale_workload" and params.get("replicas") is not None:
                target_replicas = int(params["replicas"])
            return analyse_workload_impact(
                self.k8s, target.namespace, kind, name, target_replicas=target_replicas
            )
        except Exception as exc:  # noqa: BLE001
            imp = Impact()
            imp.notes.append(f"影响面分析失败：{exc}")
            return imp

    def _signature(self, spec: ToolSpec, params: dict[str, Any], target: Target) -> list[str]:
        """当前症状签名。取不到就算了，不阻塞出方案。"""
        if spec.name in ("cordon_node", "uncordon_node", "drain_node"):
            return []  # 节点级动作不参与症状匹配
        try:
            return symptom_tokens(
                self.k8s, target.namespace,
                params.get("kind", "deployment"), params.get("name") or target.name,
            )
        except Exception:  # noqa: BLE001
            return []

    def _search_cases(self, params: dict[str, Any]) -> str:
        """按当前症状查历史案例。"""
        ns = params.get("namespace", "")
        name = params.get("name", "")
        kind = params.get("kind", "deployment")
        if self.cases is None:
            return "这台 Agent 没有配置案例库，查不到历史记录。"

        tokens = symptom_tokens(self.k8s, ns, kind, name)
        if not tokens:
            return (f"{kind}/{name} 目前没有可识别的异常症状，"
                    f"没有可匹配的历史案例。")
        hits = self.cases.search(tokens)
        header = f"当前症状：{describe(tokens)}"
        if not hits:
            self.audit.append("case_search", {
                "operator": self.operator, "namespace": ns, "name": name,
                "tokens": tokens, "hits": 0,
            })
            return (f"{header}\n案例库：没有找到**经回查确认有效**的匹配案例"
                    f"（匹配阈值 50%）。这是一次没有现成先例的排查，"
                    f"自己读证据判断。")
        body = "\n".join(c.render(score, current_workload=name) for score, c in hits)
        self.audit.append("case_search", {
            "operator": self.operator, "namespace": ns, "name": name,
            "tokens": tokens, "hits": len(hits),
            "matched": [{"workload": c.workload, "tool": c.tool,
                         "similarity": round(s, 2)} for s, c in hits],
        })
        return (f"{header}\n命中 {len(hits)} 条**回查确认有效**的历史案例：\n{body}\n\n"
                f"注意：这些是别人批准过、且事后确认症状消失的处置，"
                f"但不是你必须照做的答案，参数里的目标名要按当前对象调整。")

    def record_case(
        self,
        prop: Proposal,
        decision: Decision | None,
        result: ExecutionResult | None,
        operator: str = "",
    ) -> bool:
        """把一个案例记进库。**必须同时拿出「批准过的凭证」和「成功的结果」。**

        这个签名是刻意的：约束写在参数里，而不是靠调用方"记得只在成功时调用"。
        案例库的可信度全部来自这一条——库里每一条都是人批准过、且真的修好了的，
        而不是模型自己写进去的猜测。少了任何一个凭证就记不进去。
        """
        if result is None or result.status != "success":
            return False
        if decision is None or not decision.approved:
            return False
        if self.cases is None or not prop.signature:
            return False
        from .models import now_iso

        case = Case(
            signature=list(prop.signature),
            namespace=prop.target.namespace,
            workload=prop.target.name,
            kind=prop.target.kind,
            tool=prop.tool,
            params=dict(prop.params),
            rationale=prop.rationale,
            operator=operator or self.operator,
            # ⚠️ 这里是 applied 不是 success。
            # "API 调用成功"和"问题解决了"是两回事：内存上限改完 Pod 照样 OOM，
            # 那次调用也是 success。直接当经验教出去，下次就是一条
            # **人工批准过、却根本没用**的先例。回查之后才会变成 effective。
            outcome=OUTCOME_APPLIED,
            problems=list(prop.impact.notes),
            ts=now_iso(),
            case_id=f"case-{prop.proposal_id}",
        )
        self.cases.append(case)
        # 案例入库也留痕：知识是怎么攒起来的，要能回溯
        self.audit.append("case_recorded", {
            "operator": operator or self.operator,
            "workload": prop.target.name, "tool": prop.tool,
            "signature": prop.signature,
            "proposal_id": prop.proposal_id,
            "outcome": OUTCOME_APPLIED,
            "note": "待回查确认是否真的解决了问题",
        })
        return True

    def _dry_run(self, prop: Proposal) -> tuple[bool, str]:
        """服务端 dry-run（dry_run='All'）。不产生任何副作用。"""
        try:
            out = self.k8s.apply_mutation(prop.tool, prop.params, dry_run=True)
            return True, out
        except ActionRejected as exc:
            return False, str(exc)
        except K8sUnavailable as exc:
            return False, f"集群不可达，无法完成 dry-run：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"dry-run 异常：{exc}"

    # ---------------------------------------------------------------- 状态

    def cluster_status(self) -> tuple[bool, str]:
        return self.k8s.ping()

    def describe_self(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "write_namespaces": list(self.write_namespaces),
            "tools": {n: {"mutating": s.mutating} for n, s in TOOLS.items()},
            "forbidden": FORBIDDEN,
            "audit": str(self.audit.path),
        }
