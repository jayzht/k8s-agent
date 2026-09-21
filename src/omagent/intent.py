"""自然语言意图层：把「接口 5xx 飙升帮我看看」变成结构化查询。

为什么需要它
------------
PRD 的首要场景是"对话式运维操作"，但在这之前，入口只接受结构化参数
（`diagnose demo/api-gateway`），`AgentLoop.run(question=...)` 的 `question`
甚至是个从未被使用的死参数。用户得先自己知道是哪个工作负载出了问题——
而值班时最缺的恰恰是这个信息。

安全边界（与本项目其他部分一致）
--------------------------------
意图解析**不执行任何动作**，它的输出只是一组结构化查询参数：

1. 解析结果必须能在集群里**真实找到**，否则直接拒绝（模型不能凭空编一个工作负载名）
2. 命名空间必须在策略白名单内
3. 解析结果只喂给现有的 `diagnose → propose → 审批 → execute` 链路，
   不新增任何执行路径

换句话说：意图层最坏的情况是"找错了工作负载"，而不是"执行了错的动作"。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .k8s import K8sClient
from .policy import Policy


class IntentUnresolved(RuntimeError):
    """无法把这句话解析成一个可执行的结构化查询。"""


@dataclass
class Intent:
    """一句自然语言解析后的结构化意图。"""

    namespace: str = ""
    workload: str = ""
    kind: str = "deployment"
    planner: str = "rule"
    turns: int = 3
    confidence: str = "medium"
    interpretation: str = ""      # 用一句话复述"我理解成了什么"
    source: str = "rule"          # rule | llm
    raw: str = ""
    candidates: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "workload": self.workload,
            "kind": self.kind,
            "planner": self.planner,
            "turns": self.turns,
            "confidence": self.confidence,
            "interpretation": self.interpretation,
            "source": self.source,
            "raw": self.raw,
            "candidates": self.candidates,
        }


# ---------------------------------------------------------------------------
# 集群里有哪些工作负载（意图解析的"可选答案集合"）
# ---------------------------------------------------------------------------


def list_workloads(
    k8s: K8sClient, namespaces: list[str], kinds: tuple[str, ...] = ("deployment", "statefulset")
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ns in namespaces:
        for kind in kinds:
            try:
                if kind == "deployment":
                    items = k8s.apps.list_namespaced_deployment(ns).items
                else:
                    items = k8s.apps.list_namespaced_stateful_set(ns).items
            except Exception:  # noqa: BLE001
                continue
            for it in items:
                out.append({"namespace": ns, "kind": kind, "name": it.metadata.name})
    return out


# ---------------------------------------------------------------------------
# 规则兜底：文本里直接出现工作负载名
# ---------------------------------------------------------------------------


def _rule_parse(text: str, available: list[dict[str, str]]) -> Intent | None:
    low = (text or "").lower()
    hits = [w for w in available if w["name"].lower() in low]
    if not hits:
        # 退化尝试：按连字符切分后做前缀匹配（"api gateway" → api-gateway）
        norm = re.sub(r"[^a-z0-9]+", "", low)
        hits = [w for w in available if re.sub(r"[^a-z0-9]+", "", w["name"].lower()) in norm]

    if not hits:
        return None
    # 名字最长者优先（api-gateway 比 gateway 更具体）
    hits.sort(key=lambda w: -len(w["name"]))
    best = hits[0]
    return Intent(
        namespace=best["namespace"],
        workload=best["name"],
        kind=best["kind"],
        confidence="medium" if len(hits) == 1 else "low",
        interpretation=f"在 {best['namespace']} 命名空间诊断 {best['kind']}/{best['name']}",
        source="rule",
        raw=text,
        candidates=hits[:5] if len(hits) > 1 else [],
    )


# ---------------------------------------------------------------------------
# LLM 解析
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = """你是运维助手的前置意图解析器。

用户会用自然语言描述他遇到的问题（可能很口语、信息不全）。你的任务是把它解析成
一次**结构化诊断查询**。

## 铁律

1. **只能从"可选工作负载"列表里选**。绝对不要编造不存在的工作负载名。
2. 如果用户没有明确说是哪个工作负载，但**语义上明确指向某一个**（例如"计费服务"
   对应 `billing-core`），可以做推断，并在 `interpretation` 里说明推断依据，
   `confidence` 标为 medium。
3. **如果用户提到的服务在列表里根本不存在**（例如用户说"订单服务"，但列表里没有），
   **不要用语义相近的服务顶替**。把 `workload` 设为空字符串，并在 `missing` 里
   说明"你提到的 X 不在可诊断列表里"以及有哪些可选。
   **猜错目标比承认不知道糟糕得多**——诊断错了服务，后面所有动作都建立在错误前提上。
4. 如果**根本没法确定**是哪个工作负载（例如用户只说"集群有问题"），
   同样把 `workload` 设为空字符串，并在 `missing` 里说明还缺什么信息。
4. 你只做解析，**不执行任何操作**，也不要给出修复建议。

## 输出格式

严格输出 JSON，不要输出其他文字：

{
  "namespace": "命名空间；不确定则填空字符串",
  "kind": "deployment | statefulset",
  "workload": "工作负载名；不确定则填空字符串",
  "confidence": "high | medium | low",
  "interpretation": "用一句话复述你理解成了什么，中文",
  "missing": "若无法确定，说明还缺什么信息；否则空字符串"
}
"""


class IntentParser:
    """把自然语言解析为结构化查询。LLM 优先，不可用时退化为规则匹配。"""

    def __init__(
        self,
        k8s: K8sClient,
        policy: Policy | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 60,
    ):
        self.k8s = k8s
        self.policy = policy or Policy()
        self.base_url = (
            base_url or os.environ.get("OMAGENT_LLM_BASE_URL")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.api_key = (
            api_key or os.environ.get("OMAGENT_LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY") or ""
        )
        self.model = model or os.environ.get("OMAGENT_LLM_MODEL") or "deepseek-flash"
        self.timeout = timeout
        self.last_usage: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    # ------------------------------------------------------------------ 主入口

    def parse(self, text: str, namespace_hint: str = "") -> Intent:
        text = (text or "").strip()
        if not text:
            raise IntentUnresolved("请描述你遇到的问题，例如「api-gateway 一直重启」")

        namespaces = sorted(self.policy.allowed_namespaces)
        if namespace_hint:
            namespaces = [namespace_hint] + [n for n in namespaces if n != namespace_hint]
        if not namespaces:
            raise IntentUnresolved("策略里没有配置任何允许的命名空间")

        available = list_workloads(self.k8s, namespaces)
        if not available:
            raise IntentUnresolved(
                f"命名空间 {namespaces} 下没有找到任何工作负载"
            )

        # LLM 可用则优先；失败一律退化到规则匹配（不阻塞值班）
        if self.available:
            try:
                intent = self._llm_parse(text, available, namespaces)
                return self._validate(intent, available)
            except IntentUnresolved:
                raise
            except Exception:  # noqa: BLE001
                pass

        rule = _rule_parse(text, available)
        if rule is None:
            raise IntentUnresolved(
                "无法从这句话里识别出工作负载。"
                f"（当前可诊断：{', '.join(w['name'] for w in available[:8])}）"
                "请直接给出工作负载名。"
            )
        return self._validate(rule, available)

    # ------------------------------------------------------------------ LLM

    def _llm_parse(
        self, text: str, available: list[dict[str, str]], namespaces: list[str]
    ) -> Intent:
        catalog = "\n".join(
            f"- {w['namespace']}/{w['kind']}/{w['name']}" for w in available
        )
        prompt = (
            f"{INTENT_SYSTEM_PROMPT}\n\n"
            f"## 可选工作负载\n\n{catalog}\n\n"
            f"## 允许的命名空间\n\n{', '.join(namespaces)}\n\n"
            f"## 用户的话\n\n{text}\n"
        )
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是严谨的意图解析器，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 800,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        ) if proxy else urllib.request.build_opener())

        with opener.open(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.last_usage = data.get("usage") or {}

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        payload = json.loads(content) if content.strip() else {}

        missing = (payload.get("missing") or "").strip()
        workload = (payload.get("workload") or "").strip()
        if not workload:
            raise IntentUnresolved(
                missing or "无法确定是哪个工作负载，请补充说明。"
            )

        return Intent(
            namespace=(payload.get("namespace") or "").strip(),
            workload=workload,
            kind=(payload.get("kind") or "deployment").strip().lower(),
            confidence=(payload.get("confidence") or "medium").strip().lower(),
            interpretation=(payload.get("interpretation") or "").strip(),
            source="llm",
            raw=text,
        )

    # ------------------------------------------------------------------ 校验

    def _validate(self, intent: Intent, available: list[dict[str, str]]) -> Intent:
        """★ 关键安全性质：解析结果必须能在集群里真实找到。

        模型（或规则）编造出的工作负载名在这里被挡掉——意图层不会把
        一个不存在的东西传给下游。
        """
        match = next(
            (
                w for w in available
                if w["name"] == intent.workload
                and (not intent.namespace or w["namespace"] == intent.namespace)
            ),
            None,
        )
        if match is None:
            # 命中多个命名空间时，若名字唯一就自动定位
            same_name = [w for w in available if w["name"] == intent.workload]
            if len(same_name) == 1:
                match = same_name[0]
            else:
                near = ", ".join(w["name"] for w in available[:8])
                raise IntentUnresolved(
                    f"工作负载 {intent.workload!r} 在允许的命名空间里不存在。"
                    f"可选：{near}"
                )

        intent.namespace = match["namespace"]
        intent.workload = match["name"]
        intent.kind = match["kind"]
        if intent.confidence not in {"high", "medium", "low"}:
            intent.confidence = "medium"
        if not intent.interpretation:
            intent.interpretation = f"诊断 {intent.kind}/{intent.workload}（ns={intent.namespace}）"
        return intent
