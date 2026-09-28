"""变更历史：把「最近改了什么」直接算出来给模型。

## 为什么加这个

评测跑了 5 轮，最稳定的三个失败是 `probe` / `crash` / `cordon`，
而它们**本质是同一个盲区**：

> 模型不会把「当前症状」和「最近一次变更」联系起来，想不到"把变更退回去"。

`cordon` 那轮它甚至明确列出了"节点 cordon-drain 在自己的工具集里"，
却仍然说"硬套现有工具解决不了这个问题"。

原来的工具只能看到**此刻**：Pod 什么状态、事件说了什么、日志报了什么。
要发现"十分钟前有人把探针端口改了"，得靠模型自己把
`get_replicasets` + `get_workload` 的结果在脑子里做一次 diff——它做不到。

所以这里替它做：**列出最近的版本，并把相邻两个版本的 pod template
逐字段比出差异**。一行 `livenessProbe.httpGet.port: 8080 → 9090`，
比任何"建议你检查一下探针配置"都管用。

## 只读，且不需要任何外部系统

数据全来自 K8s 本身：Deployment/StatefulSet 的 revision annotation +
各版本 ReplicaSet 的 pod template。装 Prometheus 也没有、没装也有。
"""

from __future__ import annotations

from typing import Any

from .k8s import K8sClient
from .providers import Provider, ProviderError, ProviderTool
from .tools import ToolSpec

# 一次最多回给模型多少条差异。变更很大的发布可能几十条，
# 全塞进去会把上下文冲掉，而"前几条"通常就够了（人也是这么看的）。
MAX_DIFFS = 18
# 最多看几个历史版本
MAX_REVISIONS = 4


def _pod_spec(template: Any) -> dict[str, Any]:
    """拿到 pod template 的 spec，**键名保持 K8s 原样的驼峰**。"""
    from kubernetes.client import ApiClient

    data = ApiClient().sanitize_for_serialization(template) or {}
    spec = dict(data.get("spec") or {})
    spec.pop("$patch", None)
    return spec


def _short(value: Any, limit: int = 60) -> str:
    if value is None:
        return "（无）"
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _keyed(items: list[Any]) -> dict[str, Any] | None:
    """列表元素如果都有 name，就按 name 配对——比按下标稳。

    容器顺序变一下不该显示成"整个容器被换掉了"。
    """
    if not items or not all(isinstance(x, dict) and "name" in x for x in items):
        return None
    return {str(x["name"]): x for x in items}


def diff(a: Any, b: Any, path: str = "", out: list[tuple[str, str, str]] | None = None):
    """把两个 pod template 逐字段比出差异。返回 [(路径, 旧值, 新值)]。"""
    out = [] if out is None else out
    if len(out) >= MAX_DIFFS:
        return out

    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            diff(a.get(key, "<不存在>"), b.get(key, "<不存在>"),
                 f"{path}.{key}" if path else key, out)
        return out

    if isinstance(a, list) and isinstance(b, list):
        ka, kb = _keyed(a), _keyed(b)
        if ka is not None and kb is not None:
            for name in sorted(set(ka) | set(kb)):
                diff(ka.get(name, "<不存在>"), kb.get(name, "<不存在>"),
                     f"{path}[{name}]", out)
            return out
        if len(a) != len(b):
            out.append((path, f"{len(a)} 项", f"{len(b)} 项"))
            return out
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
        return out

    if a != b:
        out.append((path or "(根)", _short(a), _short(b)))
    return out


class ChangeHistoryProvider(Provider):
    """K8s 自带的发布历史 + pod template 差异。不需要任何外部系统。"""

    name = "changes"

    def __init__(self, k8s: K8sClient, namespace: str = "demo"):
        self.k8s = k8s
        self.namespace = namespace

    def available(self) -> tuple[bool, str]:
        try:
            self.k8s.apps.list_namespaced_deployment(self.namespace, limit=1)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"
        return True, "K8s 发布历史"

    def tools(self) -> list[ProviderTool]:
        return [
            ProviderTool(
                ToolSpec(
                    "get_recent_changes", False,
                    "看某个工作负载（或整个命名空间）**最近改了什么**："
                    "列出各发布版本，并把相邻版本的 pod template 逐字段比出差异。"
                    "排查任何「本来好好的、突然不行了」的问题都应该先调它——"
                    "症状出现前的那次改动通常就是根因。",
                    {
                        "type": "object",
                        "properties": {
                            "namespace": {"type": "string", "description": "命名空间"},
                            "name": {"type": "string",
                                     "description": "工作负载名。不填就列出该命名空间所有工作负载的最近变更"},
                        },
                    },
                ),
                self._get_recent_changes,
            ),
        ]

    # ------------------------------------------------------------------ 实现

    def _get_recent_changes(self, params: dict[str, Any]) -> str:
        ns = str(params.get("namespace") or self.namespace)
        name = str(params.get("name") or "").strip()
        try:
            if name:
                workloads = [self.k8s.apps.read_namespaced_deployment(name, ns)]
            else:
                workloads = list(self.k8s.apps.list_namespaced_deployment(ns).items)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"读不到工作负载：{type(exc).__name__}: {exc}") from exc

        if not workloads:
            return f"{ns} 下没有 Deployment。"

        blocks = []
        for w in workloads:
            block = self._describe(ns, w)
            if block:
                blocks.append(block)
        if not blocks:
            return f"{ns} 下的工作负载都只有一个版本，没有可比的变更历史。"
        return "\n\n".join(blocks)

    def _replicasets(self, ns: str, w) -> list[Any]:
        """属于这个工作负载的所有 ReplicaSet，按 revision 升序。"""
        try:
            all_rs = self.k8s.apps.list_namespaced_replica_set(ns).items
        except Exception:  # noqa: BLE001
            return []
        uid = w.metadata.uid
        mine = []
        for rs in all_rs:
            for owner in (rs.metadata.owner_references or []):
                if owner.uid == uid:
                    rev = int((rs.metadata.annotations or {}).get(
                        "deployment.kubernetes.io/revision", 0) or 0)
                    mine.append((rev, rs))
        mine.sort(key=lambda x: x[0])
        return mine

    def _describe(self, ns: str, w) -> str:
        versions = self._replicasets(ns, w)
        if len(versions) < 2:
            return ""

        tail = versions[-MAX_REVISIONS:]
        lines = [f"【{ns}/{w.metadata.name}】共 {len(versions)} 个发布版本，"
                 f"看最近 {len(tail)} 个："]

        for rev, rs in reversed(tail):     # 新的在前
            desired = rs.spec.replicas if rs.spec.replicas is not None else "?"
            ready = rs.status.ready_replicas or 0
            mark = "← 当前" if rs is versions[-1][1] else ""
            lines.append(f"  版本 {rev}（{rs.metadata.name}）："
                         f"期望 {desired} 就绪 {ready} {mark}")

        # 逐对比差异：新的在前，模型最关心"最近这一次改了什么"
        for (prev_rev, prev_rs), (rev, rs) in [
            (tail[i], tail[i + 1]) for i in range(len(tail) - 1)
        ][::-1]:
            # 必须用 sanitize_for_serialization 而不是 to_dict()：
            # to_dict() 会把字段名转成**蛇形**（liveness_probe / http_get），
            # 而 K8s API 用的是驼峰（livenessProbe / httpGet）。给模型蛇形名字，
            # 它照着写 patch 就会写错字段。
            old = _pod_spec(prev_rs.spec.template)
            new = _pod_spec(rs.spec.template)
            diffs = diff(old, new)
            if not diffs:
                continue
            lines.append(f"  ⚠️ 版本 {prev_rev} → {rev} 改了这些（pod template）：")
            for path, a, b in diffs[:MAX_DIFFS]:
                lines.append(f"      {path}: {a} → {b}")
            if len(diffs) > MAX_DIFFS:
                lines.append(f"      …… 另有 {len(diffs) - MAX_DIFFS} 处")

        return "\n".join(lines)
