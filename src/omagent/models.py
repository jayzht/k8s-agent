"""O&M Agent 核心数据契约。

设计要点（PRD 5.2）：
本系统最核心的安全属性是——一个工具**要么**永远自动放行，**要么**永远需要人工
批准。这个切分是**工具定义本身的属性**，在注册时就固定下来。规划器（或 LLM）
产出的任何内容都无法改变它。

这意味着安全性由 schema 强制，而不是靠模型"今天心情好"的自我判断。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Tier(Enum):
    """动作危险等级。数值语义见 config/policy.yaml。"""

    T0 = "T0"  # 只读，自动执行
    T1 = "T1"  # 低危写，一键确认
    T2 = "T2"  # 中危写，强确认
    T3 = "T3"  # 禁止，直接拒绝

    @property
    def rank(self) -> int:
        return {"T0": 0, "T1": 1, "T2": 2, "T3": 3}[self.value]

    @property
    def requires_approval(self) -> bool:
        return self is not Tier.T0

    @property
    def forbidden(self) -> bool:
        return self is Tier.T3

    @property
    def confirm_strength(self) -> str:
        return {"T0": "auto", "T1": "one-click", "T2": "strong", "T3": "refused"}[self.value]

    @property
    def label(self) -> str:
        return {"T0": "只读", "T1": "低危", "T2": "中危", "T3": "禁止"}[self.value]

    @classmethod
    def max_of(cls, *tiers: "Tier") -> "Tier":
        return max(tiers, key=lambda t: t.rank)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Target:
    """动作的作用对象。冻结以保证审批卡片展示的内容与执行内容一致。"""

    kind: str
    namespace: str
    name: str

    def __str__(self) -> str:
        if self.namespace:
            return f"{self.kind}/{self.name} (ns={self.namespace})"
        return f"{self.kind}/{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "namespace": self.namespace, "name": self.name}


@dataclass
class Evidence:
    """一条诊断证据。每个结论都必须挂载原始证据（PRD 4.1-B），禁止无证据结论。"""

    kind: str  # event | log | metric | state | config | probe
    ref: str
    detail: str
    source: str = ""

    def render(self) -> str:
        return f"[{self.kind}] {self.ref} — {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Impact:
    """影响面分析结果（PRD 5.4）。这是让用户"敢点确认"的核心信息。"""

    replicas: int = 0
    stateful: bool = False
    single_point: bool = False
    has_pvc: bool = False
    pdb: str = ""
    upstream_deps: list[str] = field(default_factory=list)
    pods_restarted: int = 0
    nodes_affected: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Breach:
    """熔断规则命中记录。severity=block 表示硬拒绝。"""

    rule: str
    detail: str
    severity: str = "block"  # block | escalate | warn

    def render(self) -> str:
        icon = {"block": "⛔", "escalate": "⚠️", "warn": "•"}.get(self.severity, "•")
        return f"{icon} {self.rule}: {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Proposal:
    """一个已完整评估、但**尚未执行**的动作方案。

    这是审批门禁的核心载体：规划器只能产出 Proposal，不能直接执行。
    """

    tool: str
    params: dict[str, Any]
    tier: Tier
    target: Target
    rationale: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    impact: Impact = field(default_factory=Impact)
    dry_run_ok: bool | None = None
    dry_run_output: str = ""
    rollback: str = ""
    rollback_eta: str = "未知"
    breaches: list[Breach] = field(default_factory=list)
    escalated_to: Tier | None = None  # 被熔断规则升级后的等级（原始等级仍保留在 tier）
    proposal_id: str = field(default_factory=lambda: new_id("prop-"))

    @property
    def effective_tier(self) -> Tier:
        """熔断升级后的实际等级。"""
        if self.escalated_to is None:
            return self.tier
        return Tier.max_of(self.tier, self.escalated_to)

    @property
    def blocked(self) -> bool:
        return any(b.severity == "block" for b in self.breaches)

    @property
    def requires_approval(self) -> bool:
        return self.effective_tier.requires_approval

    @property
    def dry_run_passed(self) -> bool:
        return self.dry_run_ok is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "tool": self.tool,
            "params": self.params,
            "tier": self.tier.value,
            "effective_tier": self.effective_tier.value,
            "target": self.target.to_dict(),
            "rationale": self.rationale,
            "evidence": [e.to_dict() for e in self.evidence],
            "impact": self.impact.to_dict(),
            "dry_run_ok": self.dry_run_ok,
            "dry_run_output": self.dry_run_output,
            "rollback": self.rollback,
            "rollback_eta": self.rollback_eta,
            "breaches": [b.to_dict() for b in self.breaches],
            "escalated_to": self.escalated_to.value if self.escalated_to else None,
        }


@dataclass
class Decision:
    """人对一次审批的裁决。审批人即责任人。"""

    proposal_id: str
    approved: bool
    operator: str
    reason: str = ""
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionResult:
    proposal_id: str
    status: str  # success | failed | refused | cancelled
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
