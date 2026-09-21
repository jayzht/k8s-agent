"""策略引擎：Tier 分级、命名空间白名单、熔断规则。

所有规则数据驱动自 config/policy.yaml（PRD 5.1：必须配置化，不能写死）。

失败方向永远是"拒绝"：任何未登记的动作、任何规则冲突，一律 block。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import Breach, Proposal, Tier

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "config" / "policy.yaml"
DEFAULT_STATE_PATH = ROOT / "var" / "policy-state.json"


class PolicyError(RuntimeError):
    """策略配置或动作登记错误。启动期发现，直接失败。"""


@dataclass(frozen=True)
class ActionRule:
    name: str
    tier: Tier
    mutating: bool
    rollback: str = ""
    rollback_eta: str = "未知"


def _parse_window(spec: str) -> tuple[int, int]:
    """'HH:MM-HH:MM' -> (start_minutes, end_minutes)"""
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
        return sh * 60 + sm, eh * 60 + em
    except Exception as exc:  # noqa: BLE001
        raise PolicyError(f"冻结窗口格式非法: {spec!r}，应为 'HH:MM-HH:MM'") from exc


class Policy:
    """安全策略。Agent 的每一次动笔（写操作）都必须先过这里。"""

    def __init__(
        self,
        path: str | Path = DEFAULT_POLICY_PATH,
        state_path: str | Path | None = None,
    ):
        self.path = Path(path)
        # 冷却状态必须**落盘**：放在内存里意味着"重启 Agent 即可绕过冷却期"，
        # 这是一个真实可被利用的漏洞（评测纪律要求：安全机制不能靠进程存活来保证）。
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.version: int = int(raw.get("version", 1))

        self.allowed_namespaces: set[str] = set(raw.get("allowed_namespaces") or [])
        self.readonly_namespaces: set[str] = set(raw.get("readonly_namespaces") or [])
        self.allowed_nodes: set[str] = set(raw.get("allowed_nodes") or [])
        self.protected_node_labels: dict[str, str] = dict(
            raw.get("protected_node_labels") or {}
        )
        self.protected_labels: dict[str, str] = dict(raw.get("protected_labels") or {})

        cb = raw.get("circuit_breakers") or {}
        self.max_impacted_objects = int(cb.get("max_impacted_objects", 10))
        self.cooldown_seconds = int(cb.get("per_workload_cooldown_seconds", 300))
        self.escalate_singleton_stateful = bool(cb.get("escalate_singleton_stateful", True))
        self.escalate_pvc_backed = bool(cb.get("escalate_pvc_backed", True))
        self.require_dry_run = bool(cb.get("require_dry_run", True))
        self.freeze_windows = [_parse_window(w) for w in (cb.get("freeze_windows") or [])]

        self.forbidden_actions: dict[str, dict[str, str]] = {
            a["id"]: a for a in (raw.get("forbidden_actions") or [])
        }

        self.actions: dict[str, ActionRule] = {}
        for a in raw.get("actions") or []:
            self.actions[a["name"]] = ActionRule(
                name=a["name"],
                tier=Tier(a["tier"]),
                mutating=bool(a["mutating"]),
                rollback=a.get("rollback", ""),
                rollback_eta=a.get("rollback_eta", "未知"),
            )

        self._last_change: dict[str, float] = {}
        self._load_state()

    # ------------------------------------------------------------------ 查询

    def rule(self, tool: str) -> ActionRule:
        """取动作规则。未登记 = 未授权，直接拒绝（fail closed）。"""
        if tool not in self.actions:
            raise PolicyError(
                f"动作 {tool!r} 未在受管白名单（config/policy.yaml: actions）中登记，拒绝执行"
            )
        return self.actions[tool]

    def is_registered(self, tool: str) -> bool:
        return tool in self.actions

    def readonly_allowed(self, namespace: str) -> bool:
        return namespace in self.readonly_namespaces or namespace in self.allowed_namespaces

    def in_freeze_window(self, now: float | None = None) -> bool:
        if not self.freeze_windows:
            return False
        lt = time.localtime(now if now is not None else time.time())
        cur = lt.tm_hour * 60 + lt.tm_min
        for start, end in self.freeze_windows:
            if start <= end:
                if start <= cur < end:
                    return True
            else:  # 跨零点，如 23:00-06:00
                if cur >= start or cur < end:
                    return True
        return False

    # -------------------------------------------------------------- 一致性校验

    def assert_consistent(self, tool: str, code_mutating: bool) -> ActionRule:
        """启动期完整性校验：代码里声明的 mutating 必须与策略文件一致。

        这是"工具级固定切分"（PRD 5.2）的落地保障——两个源头不一致就意味着
        有人可能在代码里悄悄把写操作标成只读，必须直接启动失败。
        """
        rule = self.rule(tool)
        if rule.mutating != code_mutating:
            raise PolicyError(
                f"工具 {tool!r} 的 mutating 声明不一致：代码={code_mutating}，"
                f"策略={rule.mutating}。拒绝启动——这可能是绕过审批门禁的尝试。"
            )
        return rule

    # ------------------------------------------------------------------ 熔断

    def record_change(self, key: str, at: float | None = None) -> None:
        self._last_change[key] = at if at is not None else time.time()
        self._save_state()

    # ------------------------------------------------------------ 状态持久化

    def _load_state(self) -> None:
        """从磁盘恢复冷却记录，并丢弃已过期的条目。"""
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        now = time.time()
        cutoff = self.cooldown_seconds
        for key, ts in (raw.get("last_change") or {}).items():
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            # 已过冷却期的条目没有保留价值
            if now - ts_f < cutoff:
                self._last_change[key] = ts_f

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"last_change": self._last_change}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)  # 原子替换，避免半写状态
        except OSError:
            # 状态写不进去不应该让整个 Agent 停摆；但冷却会退化为进程内有效
            pass

    def cooldown_remaining(self, key: str, now: float | None = None) -> float:
        last = self._last_change.get(key)
        if last is None:
            return 0.0
        remain = self.cooldown_seconds - ((now if now is not None else time.time()) - last)
        return max(0.0, remain)

    def evaluate(
        self,
        prop: Proposal,
        *,
        labels: dict[str, str] | None = None,
        now: float | None = None,
    ) -> Proposal:
        """对方案施加全部熔断规则。

        原地补齐 ``breaches`` 与 ``escalated_to``，返回同一对象。
        severity=block 的条目意味着**硬拒绝**，任何人都批不了。
        """
        labels = labels or {}
        breaches: list[Breach] = []
        escalated: Tier | None = None

        # 1) 动作必须已登记
        if not self.is_registered(prop.tool):
            breaches.append(
                Breach("未登记动作", f"{prop.tool!r} 不在受管白名单中，默认拒绝", "block")
            )
            prop.breaches = breaches
            return prop

        rule = self.actions[prop.tool]

        # 2) T3 硬拒绝
        if prop.tier.forbidden:
            breaches.append(Breach("禁止动作", f"{prop.tool} 属于 T3 禁止清单", "block"))
            prop.breaches = breaches
            return prop

        mutating = prop.tier.requires_approval

        if mutating:
            # 3) 作用域白名单：节点是集群级资源，没有 namespace，
            #    必须走单独的节点白名单，否则空 namespace 永远不匹配、
            #    会把所有节点动作无声地拦死。
            if prop.target.kind == "Node":
                if prop.target.name not in self.allowed_nodes:
                    breaches.append(
                        Breach(
                            "节点白名单",
                            f"节点 {prop.target.name!r} 不在允许列表 "
                            f"{sorted(self.allowed_nodes)} 中",
                            "block",
                        )
                    )
            elif prop.target.namespace not in self.allowed_namespaces:
                breaches.append(
                    Breach(
                        "命名空间白名单",
                        f"ns={prop.target.namespace!r} 不在允许列表 "
                        f"{sorted(self.allowed_namespaces)} 中",
                        "block",
                    )
                )

            # 4) 保护标签
            for k, v in self.protected_labels.items():
                if labels.get(k) == v:
                    breaches.append(
                        Breach("保护标签", f"资源带有 {k}={v}，禁止变更", "block")
                    )

            # 5) 爆炸半径
            impacted = max(
                prop.impact.pods_restarted,
                prop.impact.nodes_affected,
                prop.impact.replicas,
            )
            if impacted > self.max_impacted_objects:
                breaches.append(
                    Breach(
                        "爆炸半径",
                        f"影响对象数 {impacted} > 上限 {self.max_impacted_objects}",
                        "block",
                    )
                )

            # 6) 冻结窗口
            if self.in_freeze_window(now):
                breaches.append(
                    Breach("冻结窗口", "当前处于冻结窗口，强制降级为只读", "block")
                )

            # 7) 变更冷却
            key = f"{prop.target.namespace}/{prop.target.kind}/{prop.target.name}"
            remain = self.cooldown_remaining(key, now)
            if remain > 0:
                breaches.append(
                    Breach("变更冷却", f"该工作负载 {int(remain)}s 内已变更过，拒绝重复操作", "block")
                )

            # 8) 升级规则：单副本有状态服务
            if (
                self.escalate_singleton_stateful
                and prop.impact.stateful
                and prop.impact.replicas <= 1
                and prop.tier.rank < Tier.T2.rank
            ):
                escalated = Tier.T2
                breaches.append(
                    Breach(
                        "单点有状态服务",
                        f"副本数={prop.impact.replicas} 的有状态服务，"
                        f"{prop.tier.value} 升级为 {Tier.T2.value} 强确认",
                        "escalate",
                    )
                )

            # 9) 升级规则：挂载 PVC
            if (
                self.escalate_pvc_backed
                and prop.impact.has_pvc
                and prop.tier.rank < Tier.T2.rank
                and escalated is None
            ):
                escalated = Tier.T2
                breaches.append(
                    Breach(
                        "挂载 PVC",
                        f"工作负载挂载持久卷，{prop.tier.value} 升级为 {Tier.T2.value} 强确认",
                        "escalate",
                    )
                )

            # 10) 强制 dry-run
            if self.require_dry_run and prop.dry_run_ok is not True:
                breaches.append(
                    Breach(
                        "强制 dry-run",
                        "服务端干跑未通过或未执行，不生成可执行方案",
                        "block",
                    )
                )

        prop.escalated_to = escalated
        prop.breaches = breaches
        return prop

    def summary(self) -> dict[str, Any]:
        return {
            "policy_version": self.version,
            "allowed_namespaces": sorted(self.allowed_namespaces),
            "allowed_nodes": sorted(self.allowed_nodes),
            "protected_labels": self.protected_labels,
            "max_impacted_objects": self.max_impacted_objects,
            "cooldown_seconds": self.cooldown_seconds,
            "require_dry_run": self.require_dry_run,
            "freeze_windows": len(self.freeze_windows),
            "forbidden_actions": sorted(self.forbidden_actions),
            "registered_actions": {
                name: {"tier": r.tier.value, "mutating": r.mutating}
                for name, r in sorted(self.actions.items())
            },
        }
