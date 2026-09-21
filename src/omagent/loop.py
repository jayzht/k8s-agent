"""多轮取证循环（L1 Agent 的"先查后断"能力）。

为什么需要它
------------
单轮诊断隐含了一个假设：**看一眼就能给出结论和修复方案**。
但真实值班不是这样的——有经验的 SRE 会先看 Pod 状态、再看事件、再看上一次实例日志，
证据够了才下结论。

没有这个循环时会出现两种坏结果：
1. 规划器被迫"信息不足也要给方案"，产生假阳性动作；
2. 规划器说"我再看看日志"，而这在单轮里**等于什么都没做**——因为只读建议
   不会被真正执行。

本模块把三件事连起来：**规划器提出取证动作 → 真正执行（T0 自动放行）→
结果回灌给规划器 → 再判断**。

安全边界不变
------------
循环中**只能执行只读动作**。这一点由 ``OpsAgent.run_diagnostic()`` 在代码层面强制：
任何 mutating 工具走到那里都会抛 GateViolation。变更动作依然必须走
``propose() → 人工审批 → execute()``，循环无法绕过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agent import TOOLS, GateViolation, OpsAgent
from .planner import Diagnosis, Planner

# 单次诊断结果的字符上限，防止上下文被一次日志拉取撑爆（PRD 13.4 成本控制）
MAX_RESULT_CHARS = 3000


@dataclass
class DiagnosticTurn:
    """一次取证动作及其结果。"""

    turn: int
    tool: str
    params: dict[str, Any]
    result: str = ""
    error: str = ""

    def render(self) -> str:
        if self.error:
            return f"### 第 {self.turn} 轮 {self.tool}({self.params})\n[执行失败] {self.error}"
        return f"### 第 {self.turn} 轮 {self.tool}({self.params})\n{self.result}"


@dataclass
class AgentRun:
    """一次完整的"取证 → 结论"过程记录。"""

    namespace: str
    workload: str
    kind: str
    question: str = ""
    turns: list[DiagnosticTurn] = field(default_factory=list)
    diagnosis: Diagnosis | None = None
    stopped_reason: str = ""

    @property
    def rounds(self) -> int:
        return max((t.turn for t in self.turns), default=0)

    @property
    def investigated(self) -> bool:
        """是否真的执行过取证动作。"""
        return any(not t.error for t in self.turns)

    def evidence_text(self) -> str:
        return "\n\n".join(t.render() for t in self.turns)


def _is_readonly(tool: str) -> bool:
    spec = TOOLS.get(tool)
    return spec is not None and not spec.mutating


class AgentLoop:
    """把"提出取证动作 → 真正执行 → 回灌"串起来。"""

    def __init__(self, planner: Planner, agent: OpsAgent, max_turns: int = 3):
        self.planner = planner
        self.agent = agent
        self.max_turns = max_turns

    def run(
        self,
        namespace: str,
        workload: str,
        kind: str = "deployment",
        question: str = "",
    ) -> AgentRun:
        run = AgentRun(namespace=namespace, workload=workload, kind=kind, question=question)
        context: list[str] = []
        seen: set[tuple[str, str]] = set()
        final_note: str | None = None

        for round_no in range(1, self.max_turns + 1):
            diag = self._ask(namespace, workload, kind, context, final_note)
            final_note = None

            readonly_candidates = [c for c in diag.candidates if _is_readonly(c.tool)]

            # 规划器已经给出变更动作（或无需动作）→ 这就是最终结论
            if not readonly_candidates:
                run.diagnosis = diag
                run.stopped_reason = (
                    "规划器已给出变更动作" if diag.candidates else "规划器判断无需进一步取证"
                )
                return run

            # 执行本轮的取证动作（去重）
            executed = 0
            for cand in readonly_candidates:
                key = (cand.tool, _freeze(cand.params))
                if key in seen:
                    continue
                seen.add(key)
                turn = DiagnosticTurn(round_no, cand.tool, dict(cand.params))
                try:
                    turn.result = self.agent.run_diagnostic(cand.tool, cand.params)[
                        :MAX_RESULT_CHARS
                    ]
                except GateViolation as exc:
                    # 结构性保证：循环里绝不可能执行到写操作
                    turn.error = f"被拒绝（非只读动作）: {exc}"
                except Exception as exc:  # noqa: BLE001
                    turn.error = str(exc)
                run.turns.append(turn)
                context.append(turn.render())
                executed += 1

            if executed == 0:
                # 规划器重复要同样的证据 —— 不再空转，直接要它下结论
                run.stopped_reason = "规划器重复请求相同证据，强制收敛"
                run.diagnosis = self._ask(
                    namespace, workload, kind, context, "证据已齐备，请立即给出最终结论"
                )
                return run

        # 达到轮次上限 → 再要一次结论
        run.stopped_reason = f"达到最大取证轮次 {self.max_turns}"
        run.diagnosis = self._ask(
            namespace, workload, kind, context, "取证轮次已达上限，请基于现有证据给出最终结论"
        )
        return run

    def _ask(
        self,
        namespace: str,
        workload: str,
        kind: str,
        context: list[str],
        final_note: str | None,
    ) -> Diagnosis:
        """调用规划器，并把已取证结果作为附加上下文回灌。"""
        extra = "\n\n".join(context) if context else None
        diagnose = self.planner.diagnose  # type: ignore[attr-defined]
        try:
            return diagnose(
                namespace, workload, kind, extra_context=extra, final_note=final_note
            )
        except TypeError:
            # 规划器（如规则引擎）不支持额外上下文
            return diagnose(namespace, workload, kind)


def _freeze(params: dict[str, Any]) -> str:
    import json

    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
