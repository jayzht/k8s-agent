"""规则挖掘探针：让大模型生成「规则之外」的故障场景，反向测试规则引擎。

为什么这么做
------------
本项目的规则引擎在自建评测集上 100% 通过——但那是**照着自己会什么出的考卷**，
继续打磨只会过拟合。真正有价值的问题不是"它答对了多少"，
而是"**还有哪些它根本不认识**"。

而这个问题的答案，规则引擎自己产不出来（它只会匹配已知模式）。
LLM 恰好擅长"想出计划之外的情况"，所以让它来当**出题人**：
生成真实但未被规则覆盖的 K8s 故障，再拿规则引擎去考。

设计原则
--------
1. **LLM 出的期望值只是"假设"，不是真值。** 生成结果一律落到
   `evals/generated/`，标记为待人工复核，绝不自动进主用例集——
   否则就成了"用模型的答案去judge规则"，逻辑上自证。
2. **优先关注"假阳性提议"**：规则引擎提议了一个 LLM 认为不该提的动作，
   这是最危险的一类盲区（会主动造成伤害），比"少提一个动作"严重得多。
3. **生成器本身不执行任何动作**，只产出 YAML 文本。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .agent import TOOLS
from .planner import CATEGORY_LABEL, CATEGORY_ORDER
from .policy import Policy

ROOT = Path(__file__).resolve().parents[2]
GENERATED_DIR = ROOT / "evals" / "generated"


class FuzzUnavailable(RuntimeError):
    """未配置模型凭据，无法生成场景。"""


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

FUZZ_SYSTEM_PROMPT = """你是一名资深 Kubernetes SRE，正在为别人的故障诊断工具**出考卷**。

被考的系统只认识下面这些**已覆盖**的故障类别：

{known}

它可用的处置动作只有这些（不在列表里的一律不许出现）：

{actions}

## 你的任务

设计 **{n} 个真实的 K8s 故障场景**，要求：

1. **必须是真实会发生的故障**，不是臆造的。每个场景要能对应到真实的运维事故。
2. **必须落在上述已知类别之外**，或者虽然沾边但**信号会误导**已知类别。
   例如："Pod 一直重启（像 OOM），但真实原因是 ConfigMap 被误删"——
   表面信号指向已知类别，根因不在。
3. 场景必须**能从集群可观测信息中推断出来**（Pod 状态、事件、日志、规格），
   不能依赖"作者视角"才知道的信息。
4. 每个场景给出你认为**正确的处置**（或明确说明"不应该动手"）。

## 字段说明

- `fixture.pods[].last_exit_code`：容器上次退出码，没有就省略或写 null
- `fixture.pods[].ready`：是否通过就绪探针
- `fixture.pods[].reason`：如 CrashLoopBackOff / ImagePullBackOff / Unschedulable
- `fixture.pods[].termination_reason`：容器上次终止原因，只有确实被 OOM 杀才写 `OOMKilled`
- `fixture.services`：指向该工作负载的 Service 名列表
- `fixture.endpoints`：`{{服务名: 就绪后端数}}`。**这是表达"Service 层故障"的唯一方式**——
  如果你设计的场景是"Pod 都正常但服务不可达"（selector 失配、targetPort 写错），
  必须写成 `{{"你的服务名": 0}}`，否则无法被识别
- `acceptable_actions`：你认为**合理**的处置动作（可为空数组，表示不该动手）
- `forbidden_actions`：**绝对不能提**的动作（一定要包含 delete_workload、delete_namespace）
- `naive_trap`：一句话说明"一个只做关键词/状态匹配的系统会在哪里答错"——
  这是本次出题的核心价值，务必认真写

## 输出格式

严格输出 JSON 对象，`cases` 为数组，不要输出其他文字：

{{
  "cases": [
    {{
      "id": "fuzz-001",
      "title": "简短中文标题",
      "difficulty": "hard",
      "rationale": "为什么这个场景在已知类别之外",
      "naive_trap": "朴素实现会怎么答错",
      "fixture": {{
        "workload": {{"kind": "Deployment", "name": "api-gateway", "replicas": 3,
                      "labels": {{"app": "api-gateway"}}}},
        "pods": [{{"name": "api-gateway-abc", "phase": "Running", "ready": false,
                   "restarts": 5, "last_exit_code": 1, "reason": "CrashLoopBackOff",
                   "memory_limit": "256Mi"}}],
        "events": [{{"type": "Warning", "reason": "BackOff",
                     "object": "Pod/api-gateway-abc", "message": "..."}}],
        "previous_logs": "多行日志文本",
        "services": ["api-gateway"]
      }},
      "expect": {{
        "signature": "你认为的故障类型标签（可以用新词）",
        "acceptable_actions": ["rollout_undo"],
        "forbidden_actions": ["delete_workload", "delete_namespace"],
        "conclusion_any_of": ["关键词1", "关键词2"]
      }}
    }}
  ]
}}
"""


@dataclass
class FuzzResult:
    """一次生成的结果。"""

    cases: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""


class FuzzGenerator:
    """调用 LLM 生成规则之外的故障场景。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 180,
    ):
        self.base_url = (
            base_url or os.environ.get("OMAGENT_LLM_BASE_URL") or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.api_key = (
            api_key or os.environ.get("OMAGENT_LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY") or ""
        )
        self.model = model or os.environ.get("OMAGENT_LLM_MODEL") or "deepseek-flash"
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    # ------------------------------------------------------------------ 生成

    # 单次请求最多要几个场景。要得越多，输出越长，越容易把配额全耗在推理上
    # 而拿不到正文（真实踩到过：6000 token 全是 reasoning_tokens，content 为空）。
    BATCH = 3

    def generate(self, n: int = 6, policy: Policy | None = None) -> FuzzResult:
        if not self.available:
            raise FuzzUnavailable(
                "未配置模型凭据（OMAGENT_LLM_API_KEY / DEEPSEEK_API_KEY）"
            )
        policy = policy or Policy()
        all_cases: list[dict[str, Any]] = []
        usage_total: dict[str, int] = {}
        remaining = n
        round_no = 0
        while remaining > 0:
            round_no += 1
            want = min(self.BATCH, remaining)
            payload = self._generate_batch(want, policy, round_no)
            got = payload.get("cases") or []
            all_cases.extend(c for c in got if isinstance(c, dict))
            for k, v in self._last_usage.items():
                if isinstance(v, int):
                    usage_total[k] = usage_total.get(k, 0) + v
            if not got:
                break            # 拿不到更多就停，不空转
            remaining -= len(got)
        return FuzzResult(
            cases=self._clean(all_cases), usage=usage_total, model=self.model
        )

    def _generate_batch(self, want: int, policy: Policy, round_no: int) -> dict[str, Any]:
        known = "\n".join(f"- {sig}（{CATEGORY_LABEL[sig]}）" for sig in CATEGORY_ORDER)
        actions = "\n".join(
            f"- {name}（{policy.actions[name].tier.value} {policy.actions[name].tier.label}）"
            for name in sorted(TOOLS)
            if name in policy.actions and not policy.actions[name].tier.forbidden
        )
        prompt = FUZZ_SYSTEM_PROMPT.format(known=known, actions=actions, n=want)
        if round_no > 1:
            prompt += f"\n\n（这是第 {round_no} 批，请设计与前面不同的场景。）"
        return self._call(prompt)

    def _clean(self, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for i, c in enumerate(cases, 1):
            c = dict(c)
            c["id"] = f"fuzz-{i:03d}-{_slug(c.get('title', str(i)))}"
            c.setdefault("difficulty", "hard")
            cleaned.append(c)
        return cleaned

    _last_usage: dict[str, Any] = {}

    def _call(self, prompt: str) -> dict[str, Any]:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是资深 SRE，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,      # 出题需要多样性，这里刻意不取 0
            "max_tokens": 16000,
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
        self._last_usage = data.get("usage") or {}
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if not content.strip():
            raise RuntimeError(f"模型返回空内容（usage={self._last_usage}）")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            stripped = content.strip().removeprefix("```json").removeprefix("```")
            return json.loads(stripped.removesuffix("```").strip())


def _slug(text: str) -> str:
    keep = [ch for ch in str(text) if ch.isalnum()]
    return ("".join(keep))[:16] or "case"


def save_generated(cases: list[dict[str, Any]], name: str = "round1") -> Path:
    """把生成的场景写入 evals/generated/（**待人工复核**，不自动进主用例集）。"""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out = GENERATED_DIR / f"{name}.yaml"
    header = (
        "# ⚠️ 由大模型自动生成的候选用例，**尚未人工复核**\n"
        "#\n"
        "# 用途：发现规则引擎的盲区（见 src/omagent/fuzz.py）。\n"
        "# 注意：这里的 expect 是**模型的假设**，不是真值。\n"
        "# 复核通过后才应移入 evals/cases/，否则等于用模型的答案去 judge 规则。\n\n"
    )
    out.write_text(header + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 盲区分类：拿规则引擎去考这些新场景
# ---------------------------------------------------------------------------

# 严重度：假阳性最危险（会主动造成伤害），其次是漏报，最后是"不认识"
SEVERITY = {"false_positive": 0, "miss": 1, "unrecognized": 2, "ok": 3}
SEVERITY_LABEL = {
    "false_positive": "🚨 假阳性提议（最危险）",
    "miss": "⚠️ 漏报（该动手没动手）",
    "unrecognized": "· 特征未识别",
    "ok": "✓ 通过",
}


@dataclass
class BlindSpot:
    case_id: str
    title: str
    verdict: str
    expected_signature: str = ""
    actual_signature: str = ""
    proposed: list[str] = field(default_factory=list)
    acceptable: list[str] = field(default_factory=list)
    naive_trap: str = ""
    rationale: str = ""
    detail: str = ""

    def render(self) -> str:
        head = f"[{self.case_id}] {self.title}"
        lines = [head, f"    {SEVERITY_LABEL[self.verdict]}"]
        if self.verdict == "unrecognized":
            lines.append(
                f"    期望类别={self.expected_signature}  实际={self.actual_signature}"
            )
        if self.proposed:
            lines.append(f"    规则引擎提议：{self.proposed}")
        if self.detail:
            lines.append(f"    {self.detail}")
        if self.naive_trap:
            lines.append(f"    💡 {self.naive_trap}")
        return "\n".join(lines)


def analyse_generated(cases: list[dict[str, Any]]) -> list[BlindSpot]:
    """对每个生成场景跑一遍规则引擎，判定属于哪类盲区。

    注意：``verdict`` 里的"假阳性/漏报"是**相对于模型给出的期望值**而言的。
    模型的期望值只是假设，所以这些结论是"待复核的线索"，不是判决。
    但 **假阳性线索值得优先看**——即使模型期望值有偏差，
    "规则引擎提议了一个动作"这件事本身也需要人确认是否恰当。
    """
    from .evals import EvalCase, run_case

    results: list[BlindSpot] = []
    for raw in cases:
        try:
            case = EvalCase.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            results.append(BlindSpot(
                case_id=str(raw.get("id", "?")), title=str(raw.get("title", "")),
                verdict="unrecognized", detail=f"用例结构非法，已跳过：{exc}",
            ))
            continue

        r = run_case(case)
        bs = BlindSpot(
            case_id=case.id,
            title=case.title,
            verdict="ok",
            expected_signature=case.expect_signature,
            actual_signature=r.signature_actual,
            proposed=r.mutating_proposals,
            acceptable=case.acceptable_actions,
            naive_trap=str(raw.get("naive_trap", "")),
            rationale=str(raw.get("rationale", "")),
        )

        if r.dangerous_proposals:
            # 注意措辞：用例里的 forbidden_actions 表示"本场景不该用这个动作"，
            # 而不是"这是 T3 安全禁止动作"。两者性质不同，不能混为一谈。
            bs.verdict = "false_positive"
            bs.detail = (
                f"提议了本场景不应使用的动作：{r.dangerous_proposals}"
                f"（模型认为此处不该用）"
            )
        elif (
            r.mutating_proposals
            and case.acceptable_actions
            and not set(r.mutating_proposals) & set(case.acceptable_actions)
        ):
            # 提了动作，但都不在"合理动作"里 —— 可能是假阳性，也可能模型期望值偏窄
            bs.verdict = "false_positive"
            bs.detail = (
                f"提议 {r.mutating_proposals}，但模型认为合理的是 {case.acceptable_actions}"
                "（需人工判断谁对）"
            )
        elif not r.action_coverage:
            # 覆盖判定沿用 harness 的语义（含只读动作）：
            # 模型说"该做 get_endpoints"，引擎确实提了 get_endpoints，就算覆盖到了。
            bs.verdict = "miss"
            bs.detail = (
                f"模型认为应做 {case.acceptable_actions}，"
                f"规则引擎实际提议 {r.proposed_actions or '（无）'}"
            )
        elif r.signature_actual != case.expect_signature:
            # 标签不同但**行为正确**——生成的用例里模型会自造分类名
            # （如 service_selector_mismatch_empty_endpoints），
            # 不该因为名字对不上就判失败。只在信息里记一笔。
            bs.verdict = "ok"
            bs.detail = (
                f"行为正确；分类标签不同（模型用 {case.expect_signature!r}，"
                f"引擎用 {r.signature_actual!r}）"
            )
        results.append(bs)

    results.sort(key=lambda b: (SEVERITY[b.verdict], b.case_id))
    return results


def to_markdown(spots: list[BlindSpot], title: str = "规则引擎盲区报告") -> str:
    """把盲区分析渲染成 Markdown，便于贴进文档或 issue。"""
    counts = summarise(spots)
    lines = [
        f"# {title}",
        "",
        "> 场景由大模型生成（`omagent fuzz`），期望值是**模型假设**，需人工复核。",
        "",
        "| 判定 | 数量 |",
        "|---|---|",
        f"| 🚨 假阳性提议 | {counts.get('false_positive', 0)} |",
        f"| ⚠️ 漏报 | {counts.get('miss', 0)} |",
        f"| · 特征未识别 | {counts.get('unrecognized', 0)} |",
        f"| ✓ 通过 | {counts.get('ok', 0)} |",
        "",
        "---",
        "",
    ]
    for sp in spots:
        if sp.verdict == "ok":
            continue
        lines += [
            f"## [{sp.case_id}] {sp.title}",
            "",
            f"**判定**：{SEVERITY_LABEL[sp.verdict]}",
            "",
        ]
        if sp.expected_signature or sp.actual_signature:
            lines += [
                f"- 期望类别：`{sp.expected_signature}`",
                f"- 实际类别：`{sp.actual_signature}`",
            ]
        if sp.proposed:
            lines.append(f"- 规则引擎提议：`{sp.proposed}`")
        if sp.acceptable:
            lines.append(f"- 模型认为合理：`{sp.acceptable}`")
        if sp.detail:
            lines += ["", f"> {sp.detail}"]
        if sp.rationale:
            lines += ["", f"**为什么在已知类别之外**：{sp.rationale}"]
        if sp.naive_trap:
            lines += ["", f"**朴素实现会怎么答错**：{sp.naive_trap}"]
        lines.append("")
    return "\n".join(lines)


def summarise(spots: list[BlindSpot]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in spots:
        out[s.verdict] = out.get(s.verdict, 0) + 1
    return out
