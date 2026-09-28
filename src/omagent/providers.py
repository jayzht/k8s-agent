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

import threading
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
    """provider 提供的一个工具：一份 schema + 执行体。

    **只读工具**：给 ``call`` 就行。
    **写工具**：必须同时给 ``dry_run`` 和 ``apply``——这是能注册为写工具的前提。

    为什么写工具必须有 ``dry_run``：整个项目的安全承诺是
    「**服务端干跑没过，人工批准也不执行**」。一个提供不了无副作用预检的写工具，
    就没法纳入这条承诺，因此不允许注册。这不是"暂时不支持"，是**刻意的门槛**。
    """

    spec: ToolSpec
    call: Callable[[dict[str, Any]], str] | None = None
    # 写工具必填：dry_run 返回 (是否通过, 说明)；apply 真正落地并返回人话结果
    dry_run: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    apply: Callable[[dict[str, Any]], str] | None = None

    @property
    def has_write_path(self) -> bool:
        return self.dry_run is not None and self.apply is not None


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
        """给人看的补充信息。``skipped`` 用于说明"这个源里有工具我没导入"。"""
        return {"name": self.name, "skipped": []}


class ProviderRegistry:
    """把若干 provider 装配成一张只读工具表。

    原来装配是**一次性**的（启动时问一遍可用性，之后不变），好处是"这次启动
    到底暴露了哪些工具"是个确定的事实。但那样就没法在页面直接加工具，
    只能改命令行再重启。

    现在支持运行时增删，代价是要用锁保护——因为 HTTP 线程会在会话跑着的时候
    改它。**加锁只解决数据竞争，不解决语义**：正在跑的会话可能刚好用到一个
    被移除的工具。那种情况会明确报"工具未登记"，不会静默用错工具。
    """

    def __init__(self, providers: list[Provider] | None = None):
        self.available: dict[str, Provider] = {}
        self.unavailable: dict[str, str] = {}
        self._tools: dict[str, tuple[Provider, ProviderTool]] = {}
        self._lock = threading.RLock()

        for p in providers or []:
            self.add(p)

    # ------------------------------------------------------------ 运行时增删

    def add(self, provider: Provider) -> tuple[bool, str]:
        """装配一个 provider。返回 (是否可用, 原因)。

        不可用时**不抛异常**——外部依赖连不上是常态，注册表只把它记进
        ``unavailable`` 并说明原因，界面会显示出来。
        但**契约违反要抛**：声明写工具却没有 dry_run，是代码错误，不能静默放过。
        """
        with self._lock:
            if provider.name in self.available:
                self.remove(provider.name)
            try:
                ok, why = provider.available()
            except Exception as exc:  # noqa: BLE001
                ok, why = False, f"{type(exc).__name__}: {exc}"
            if not ok:
                self.unavailable[provider.name] = why
                return False, why

            # 先把工具清单和 schema 校验跑完，**再**往表里放——
            # 中途抛异常的话，不能留下半张表。
            staged: list[tuple[str, ProviderTool]] = []
            for t in provider.tools():
                if t.spec.mutating and not t.has_write_path:
                    raise UnsafeProvider(
                        f"provider {provider.name!r} 把 {t.spec.name!r} 声明成写工具，"
                        "却没提供 dry_run / apply。"
                        "整个项目的安全承诺是「服务端干跑没过，人工批准也不执行」——"
                        "提供不了无副作用预检的写工具，不能纳入这条承诺，因此不允许注册。"
                    )
                if not t.spec.mutating and t.has_write_path:
                    raise UnsafeProvider(
                        f"provider {provider.name!r} 把 {t.spec.name!r} 声明成只读，"
                        "却带了 dry_run / apply。声明和实现自相矛盾，不能猜哪个是真的。"
                    )
                if not t.spec.mutating and t.call is None:
                    raise UnsafeProvider(
                        f"provider {provider.name!r} 的只读工具 {t.spec.name!r} 没有 call 执行体。"
                    )
                if t.spec.name in self._tools:
                    owner = self._tools[t.spec.name][0].name
                    raise ValueError(
                        f"工具名冲突：{t.spec.name!r} 已由 provider {owner!r} 提供"
                    )
                staged.append((t.spec.name, t))

            for name, t in staged:
                self._tools[name] = (provider, t)
            self.available[provider.name] = provider
            self.unavailable.pop(provider.name, None)
            return True, why

    def remove(self, name: str) -> bool:
        """摘掉一个 provider 连同它的工具。"""
        with self._lock:
            p = self.available.pop(name, None)
            self.unavailable.pop(name, None)
            if p is None:
                return False
            for tool in [t for t, (pr, _x) in self._tools.items() if pr.name == name]:
                del self._tools[tool]
            closer = getattr(p, "close", None)
            if callable(closer):
                try:
                    closer()          # MCP provider 要收掉子进程
                except Exception:  # noqa: BLE001
                    pass
            return True

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

    def is_mutating(self, name: str) -> bool:
        """未登记的仍然 fail closed（返回 True）。"""
        spec = self.spec(name)
        return True if spec is None else spec.mutating

    def dry_run(self, name: str, params: dict[str, Any]) -> tuple[bool, str]:
        """走 provider 写工具的无副作用预检。"""
        entry = self._tools.get(name)
        if entry is None or entry[1].dry_run is None:
            raise ProviderError(f"{name!r} 不是已注册的 provider 写工具")
        return entry[1].dry_run(params)

    def apply(self, name: str, params: dict[str, Any]) -> str:
        entry = self._tools.get(name)
        if entry is None or entry[1].apply is None:
            raise ProviderError(f"{name!r} 不是已注册的 provider 写工具")
        return entry[1].apply(params)

    # ------------------------------------------------------- 给模型 / 界面用

    def openai_schema(self) -> list[dict[str, Any]]:
        return [
            {"type": "function",
             "function": {"name": t.spec.name,
                          "description": f"[{p.name}] {t.spec.summary}"
                          + ("（写操作：调用后不会立即执行，需要运维人员确认）"
                             if t.spec.mutating else ""),
                          "parameters": t.spec.schema}}
            for p, t in self._tools.values()
        ]

    def ui_list(self) -> list[dict[str, Any]]:
        return [
            {"name": t.spec.name, "mutating": t.spec.mutating,
             "summary": f"[{p.name}] {t.spec.summary}", "provider": p.name}
            for p, t in self._tools.values()
        ]

    def status(self) -> list[dict[str, Any]]:
        """自检/界面用：装了哪些、跳过了哪些、为什么。"""
        out: list[dict[str, Any]] = []
        for name, p in self.available.items():
            info = p.describe()
            out.append({
                "name": name, "available": True,
                "tools": sum(1 for pr, _t in self._tools.values() if pr.name == name),
                "reason": "",
                # 例如 MCP：连上了、有 8 个工具，但只导入了白名单里的 1 个。
                # 不说清楚的话，人会以为"接上了怎么啥也没有"。
                "skipped": info.get("skipped", []),
            })
        for name, why in self.unavailable.items():
            out.append({"name": name, "available": False, "tools": 0,
                        "reason": why, "skipped": []})
        return out
