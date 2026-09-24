"""核心数据契约。

设计要点（只有一条，但它是整个产品的地基）：

    一个工具**要么**永远自动执行，**要么**永远需要人工批准。
    这个切分是**工具定义本身的属性**（见 ``tools.py``），在注册时就固定下来。
    模型产出的任何内容都无法改变它。

没有分级、没有熔断、没有配置化的风险矩阵。只有"读"和"写"两档，
因为运维的人话就是这么分的：**看东西不用问，动东西要问。**
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Target:
    """动作的作用对象。

    冻结（frozen）是刻意的：审批卡片上展示的目标与真正执行的目标必须是同一个值，
    否则会出现"批准 A、执行 B"。冻结让这种篡改在类型层面就写不出来。
    """

    kind: str
    namespace: str
    name: str

    def __str__(self) -> str:
        if self.namespace:
            return f"{self.kind}/{self.name} (ns={self.namespace})"
        return f"{self.kind}/{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "namespace": self.namespace, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Target":
        return cls(kind=d.get("kind", ""), namespace=d.get("namespace", ""),
                   name=d.get("name", ""))


@dataclass
class Impact:
    """影响面分析结果：让操作员"敢点确认"的关键信息。

    ``replicas`` 是**当前**副本数，``target_replicas`` 是这次变更**之后**会变成多少。
    两个都要有：把 3 缩到 1 和把 3 扩到 10，在卡片上长得一模一样的话，
    操作员看到「影响副本 3 / 是否单点 否」就会以为没事——而改完之后它就是单点了。
    最需要准确的那张卡片，不能给错信息。
    """

    replicas: int = 0
    target_replicas: int | None = None
    pods_added: int = 0
    pods_removed: int = 0
    stateful: bool = False
    single_point: bool = False
    has_pvc: bool = False
    pdb: str = ""
    upstream_deps: list[str] = field(default_factory=list)
    pods_restarted: int = 0
    nodes_affected: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def changes_replica_count(self) -> bool:
        return self.target_replicas is not None and self.target_replicas != self.replicas

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Impact":
        """从磁盘恢复。

        未知字段一律忽略、缺字段用默认值——这样**旧版本的会话文件在新版本代码上
        仍然读得出来**。会话是持久化的，结构演进时不该因为加了个字段就全部作废。
        """
        if not d:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Proposal:
    """一个已评估、但**尚未执行**的动作方案。

    它是审批门禁的核心载体：模型只能产出 Proposal，不能执行任何东西。
    """

    tool: str
    params: dict[str, Any]
    target: Target
    rationale: str = ""
    impact: Impact = field(default_factory=Impact)
    dry_run_ok: bool | None = None
    dry_run_output: str = ""
    rollback: str = ""
    proposal_id: str = field(default_factory=lambda: new_id("prop-"))
    # 提方案那一刻算出来的症状签名。执行成功后用它把这条件记进案例库——
    # 记的是"当时看到的症状"，不是执行完之后的（执行完症状就没了）。
    signature: list[str] = field(default_factory=list)

    @property
    def dry_run_passed(self) -> bool:
        return self.dry_run_ok is True

    @property
    def display_command(self) -> str:
        """给操作员看的一行"这到底要干什么"。不是可执行的 shell，是给人读的摘要。"""
        parts = [f"{k}={v}" for k, v in self.params.items() if v not in (None, "")]
        return f"{self.tool}({', '.join(parts)})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "tool": self.tool,
            "params": self.params,
            "target": self.target.to_dict(),
            "rationale": self.rationale,
            "impact": self.impact.to_dict(),
            "dry_run_ok": self.dry_run_ok,
            "dry_run_output": self.dry_run_output,
            "rollback": self.rollback,
            "display_command": self.display_command,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        """从磁盘恢复。

        注意：恢复出来的 Proposal **只是展示用的记录**。真正执行时仍然会走
        ``execute_write()`` 的完整门禁（审批凭证匹配 + 重新 dry-run），
        所以从磁盘读一个方案出来并不构成一条绕过门禁的路径。
        """
        return cls(
            tool=d.get("tool", ""),
            params=dict(d.get("params") or {}),
            target=Target.from_dict(d.get("target") or {}),
            rationale=d.get("rationale", ""),
            impact=Impact.from_dict(d.get("impact")),
            dry_run_ok=d.get("dry_run_ok"),
            dry_run_output=d.get("dry_run_output", ""),
            rollback=d.get("rollback", ""),
            proposal_id=d.get("proposal_id") or new_id("prop-"),
            signature=list(d.get("signature") or []),
        )


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
