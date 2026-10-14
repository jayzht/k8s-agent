"""巡检与告警接入：让系统从"人来问"变成"主动发现"。

## 这是这个项目最大的一块缺口

按运维工作内容粗算，「监控告警响应 / 巡检」占 **~30%**，是单项占比最高的一块，
而原来的系统**完全没碰**——它的形态是"人来问"，不是"主动报"。

## 但架构上有一条不能破的线

**主动巡检 ≠ 自主执行。**

```
自动触发（定时 / webhook）
      ↓
只读调查（自动执行，本来就不需要许可）
      ↓
产出仍然是一张**待批卡片**
```

审批门禁一步都不挪。巡检改变的是"谁先发现问题"，不是"谁有权改集群"。
如果让巡检能自己执行，那前面十一轮做的读写切分、dry-run、审批、审计全部作废。

## 为什么不直接用 LLM 定时扫全集群

太贵，而且没必要。绝大多数时刻集群是好的。

所以分两级：

1. **确定性预筛（每次巡检都跑，零模型成本）**：直接复用监控台那套
   ``console.overview()``——它已经把每个负载的 ``healthy`` 算出来了。
2. **只对异常调模型**：异常的工作负载才起一次调查。

## 去重是必需的，不是优化

一个坏掉的工作负载会**每一次巡检都异常**。不去重的话：
- 十分钟一次巡检 → 一天 144 次同样的调查
- 模型成本爆炸，控制台被同一个问题刷屏，人反而看不见新问题

所以每个目标有一个冷却期，期内不重复报。**"一直在坏"本身不是新信息。**
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import console as console_mod


@dataclass
class Finding:
    """一次巡检发现的异常。**纯确定性算出来的，没调模型。**"""

    namespace: str
    kind: str
    name: str
    problems: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.kind}/{self.name}"

    def question(self) -> str:
        """交给 Agent 的问题。把已算出的现象一起给它，省它重查一遍。"""
        why = "；".join(self.problems[:4]) or "状态异常"
        return (f"{self.namespace} 下的 {self.kind}/{self.name} 巡检时发现异常：{why}。"
                f"请查清楚原因，并给出处置方案。")


class Patrol:
    """定期扫集群，把**新出现的**异常交给 Agent 去查。

    它自己不做任何变更，也不直接调模型——只负责"发现"和"去重"，
    调查交给回调（由 App 起一个会话），产出仍然是待批卡片。
    """

    def __init__(
        self,
        k8s: Any,
        namespaces: list[str],
        *,
        interval: float = 300.0,
        cooldown: float = 1800.0,
        on_finding: Callable[[Finding], None] | None = None,
        cases: Any = None,
    ):
        self.k8s = k8s
        self.namespaces = list(namespaces)
        self.interval = max(30.0, float(interval))
        # 冷却期：同一个目标在这个时间内不重复报。
        # 默认 30 分钟——比巡检间隔大得多，因为"一直在坏"不是新信息。
        self.cooldown = max(0.0, float(cooldown))
        self.on_finding = on_finding
        self.cases = cases

        self._last_seen: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 给人看的统计
        self.scans = 0
        self.findings_total = 0
        self.suppressed = 0

    # ------------------------------------------------------------------ 扫描

    def scan_once(self) -> list[Finding]:
        """跑一遍巡检。**纯确定性，不调模型。** 不可达的命名空间跳过。"""
        with self._lock:
            self.scans += 1
        out: list[Finding] = []
        for ns in self.namespaces:
            try:
                view = console_mod.overview(self.k8s, ns, cases=self.cases)
            except Exception:  # noqa: BLE001
                continue      # 某个命名空间读不到不该让整轮巡检失败
            if not view.get("ok", True):
                continue
            for row in view.get("workloads") or []:
                if row.get("healthy"):
                    continue
                out.append(Finding(
                    namespace=ns,
                    kind=str(row.get("kind") or "deployment"),
                    name=str(row.get("name") or ""),
                    problems=list(row.get("problems") or []),
                ))
        return [f for f in out if f.name]

    def new_findings(self, findings: list[Finding], *, now: float | None = None) -> list[Finding]:
        """过滤掉冷却期内的重复项，并**只对新的记时间**。

        注意是在**过滤之后**才更新时间戳：不然每个巡检周期都会把时间戳刷新，
        冷却期永远也走不完，就变成"只要还坏着就一直不报"——那是另一个极端。
        """
        now = time.time() if now is None else now
        fresh: list[Finding] = []
        with self._lock:
            for f in findings:
                last = self._last_seen.get(f.key)
                if last is not None and (now - last) < self.cooldown:
                    self.suppressed += 1
                    continue
                self._last_seen[f.key] = now
                fresh.append(f)
            self.findings_total += len(fresh)
        return fresh

    def tick(self) -> list[Finding]:
        """一个巡检周期：扫描 → 去重 → 把新的交给回调。"""
        fresh = self.new_findings(self.scan_once())
        for f in fresh:
            if self.on_finding is not None:
                try:
                    self.on_finding(f)
                except Exception:  # noqa: BLE001
                    pass      # 单个调查起不来不该打断整轮巡检
        return fresh

    # ------------------------------------------------------------ 告警接入

    def ingest_alerts(self, payload: dict[str, Any]) -> list[Finding]:
        """接 Alertmanager webhook。

        它和定时巡检**走同一条去重路径**——否则告警和巡检会对同一个故障
        各报一次，人看到两张卡。

        Alertmanager 的 payload 形态：``{"alerts": [{"labels": {...}, "annotations": {...}}]}``
        另外兼容更简单的 ``{"namespace": "...", "workload": "..."}``——方便手工触发。
        """
        findings: list[Finding] = []
        for a in payload.get("alerts") or []:
            labels = a.get("labels") or {}
            ann = a.get("annotations") or {}
            ns = labels.get("namespace") or payload.get("namespace") or ""
            name = labels.get("deployment") or labels.get("workload") or labels.get("pod") or ""
            if not ns or not name:
                continue
            findings.append(Finding(
                namespace=str(ns), kind="deployment", name=str(name),
                problems=[str(ann.get("summary") or labels.get("alertname") or "告警触发")],
            ))
        # 手工触发的简单形态
        if not findings and payload.get("namespace") and payload.get("workload"):
            findings.append(Finding(
                namespace=str(payload["namespace"]), kind="deployment",
                name=str(payload["workload"]),
                problems=[str(payload.get("reason") or "手工触发")],
            ))
        return self.new_findings(findings)

    # ------------------------------------------------------------ 后台循环

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="patrol", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                pass      # 巡检挂了不能让整个服务跟着挂

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._thread is not None,
            "interval_seconds": int(self.interval),
            "cooldown_seconds": int(self.cooldown),
            "namespaces": list(self.namespaces),
            "scans": self.scans,
            "findings_total": self.findings_total,
            "suppressed": self.suppressed,
            "watching": sorted(self._last_seen),
        }
