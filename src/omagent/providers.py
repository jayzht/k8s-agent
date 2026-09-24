"""可插拔的数据源 provider。

## 为什么需要这一层

在此之前，所有工具都是 K8s 的，而且都塞在 ``K8sClient`` 里。要接 Prometheus，
只能往一个"Kubernetes 客户端"里硬塞 HTTP 调用——架构上说不通，而且加一个工具
要同时改 ``tools.py`` 和 ``k8s.py`` 两处。

这层缝把"数据源"变成可插拔的：provider 自报可用性和工具清单，注册表把它们
拼进 agent 的工具集。加一个数据源 = 加一个类，不动核心。

## 安全不变式（这一条比扩展性重要）

**provider 只能提供只读工具。** 写操作必须走 ``K8sClient.apply_mutation``
——那是唯一被门禁、dry-run 复跑、命名空间白名单和哈希链审计覆盖的路径。
让 provider 自带写工具，等于开一条绕过整套审批的后门。

所以注册表在**构造时**就拒绝任何 mutating 的 provider 工具并直接抛异常，
而不是打个日志放过。扩展数据源不该降低系统的安全下限——这条和我
「未登记的工具一律当写操作」是同一个思路：宁可拒绝，也不放行一个
没说清楚的东西。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from .tools import ToolSpec


class ProviderError(RuntimeError):
    """provider 自身的调用失败（网络、认证、查询语法等）。"""


class UnsafeProvider(RuntimeError):
    """provider 试图提供写工具——这是设计上不允许的。"""


@dataclass(frozen=True)
class ProviderTool:
    """provider 提供的一个只读工具：一份 schema + 一个执行函数。"""

    spec: ToolSpec
    call: Callable[[dict[str, Any]], str]


class Provider(ABC):
    """一个数据源。实现 ``available()`` 和 ``tools()`` 即可。

    ``available()`` 返回 ``(是否可用, 原因)``。不可用时注册表只是**跳过**它，
    不影响其它 provider 更不影响内置 K8s 工具——外部依赖缺失应该是功能降级，
    不该让整个 agent 起不来。
    """

    name: str = "provider"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """现在能用吗？不能用就给出人能看懂的原因。"""

    @abstractmethod
    def tools(self) -> list[ProviderTool]:
        """本 provider 提供的工具（**必须全部是只读的**）。"""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}


class ProviderRegistry:
    """把若干 provider 装配成一张只读工具表。

    装配是**一次性**的：构造时问一遍可用性，之后不再变。这样"这次启动到底
    暴露了哪些工具"是个确定的事实，而不是每次调用都可能变的运行时状态
    ——审计和自检都需要它是个事实。
    """

    def __init__(self, providers: list[Provider] | None = None):
        self.available: dict[str, Provider] = {}
        self.unavailable: dict[str, str] = {}
        self._tools: dict[str, tuple[Provider, ProviderTool]] = {}

        for p in providers or []:
            try:
                ok, why = p.available()
            except Exception as exc:  # noqa: BLE001
                ok, why = False, f"{type(exc).__name__}: {exc}"
            if not ok:
                self.unavailable[p.name] = why
                continue
            for t in p.tools():
                # fail closed：写工具一律拒绝装配，不做"打个日志放过"这种事
                if t.spec.mutating:
                    raise UnsafeProvider(
                        f"provider {p.name!r} 试图提供写工具 {t.spec.name!r}。"
                        "provider 只能是只读数据源；写操作必须走 K8sClient.apply_mutation，"
                        "否则会绕过审批、dry-run 和审计。"
                    )
                if t.spec.name in self._tools:
                    owner = self._tools[t.spec.name][0].name
                    raise ValueError(
                        f"工具名冲突：{t.spec.name!r} 已由 provider {owner!r} 提供"
                    )
                self._tools[t.spec.name] = (p, t)
            self.available[p.name] = p

    # ------------------------------------------------------------------ 查询

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def spec(self, name: str) -> ToolSpec | None:
        entry = self._tools.get(name)
        return entry[1].spec if entry else None

    def specs(self) -> list[ToolSpec]:
        return [t.spec for _p, t in self._tools.values()]

    def provider_of(self, name: str) -> str | None:
        entry = self._tools.get(name)
        return entry[0].name if entry else None

    def call(self, name: str, params: dict[str, Any]) -> str:
        """执行一个 provider 工具。未登记的名字直接拒绝（fail closed）。"""
        entry = self._tools.get(name)
        if entry is None:
            raise ProviderError(f"provider 工具 {name!r} 未登记")
        p, t = entry
        try:
            return t.call(params)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{p.name}: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------- 给模型 / 界面用

    def openai_schema(self) -> list[dict[str, Any]]:
        return [
            {"type": "function",
             "function": {"name": t.spec.name,
                          "description": f"[{p.name}] {t.spec.summary}",
                          "parameters": t.spec.schema}}
            for p, t in self._tools.values()
        ]

    def ui_list(self) -> list[dict[str, Any]]:
        return [
            {"name": t.spec.name, "mutating": False,
             "summary": f"[{p.name}] {t.spec.summary}", "provider": p.name}
            for p, t in self._tools.values()
        ]

    def status(self) -> list[dict[str, Any]]:
        """自检/界面用：装了哪些、跳过了哪些、为什么。"""
        out: list[dict[str, Any]] = []
        for name, p in self.available.items():
            out.append({"name": name, "available": True,
                        "tools": sum(1 for pr, _t in self._tools.values()
                                     if pr.name == name),
                        "reason": ""})
        for name, why in self.unavailable.items():
            out.append({"name": name, "available": False, "tools": 0, "reason": why})
        return out
