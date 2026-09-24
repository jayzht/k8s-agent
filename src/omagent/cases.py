"""语义记忆：结构化的"症状 → 处置 → 结果"案例库。

为什么是**结构化匹配**，而不是向量检索
======================================

这个选择值得说清楚，因为"给 Agent 加记忆"现在最常见的默认答案是 RAG。

1. **这个领域的故障是可归类的，而且签名能确定性地算出来。**
   OOMKilled / CrashLoopBackOff / ImagePullBackOff / Pending / 探针失败 /
   没有后端 / targetPort 对不上……这些不是"语义相似"，是**从 K8s 对象上
   直接读出来的事实**。既然能精确算出来，就没有理由去猜一个余弦相似度。

2. **运维要的是"上次这个我怎么修的"，不是"一段语义相关的文字"。**
   精确匹配能回答："同样的症状，上次用 `patch_resources(memory_limit=128Mi)`
   修好了，执行成功。" 这比召回一个相似段落有用得多。

3. **可解释性，而且是能进审计的那种。**
   一个会改生产的系统，"为什么模型这么说"必须答得上来。
   结构化匹配的答案是"症状签名完全相同，这是那次的人工批准记录"；
   向量检索的答案是"因为余弦相似度 0.83"。前者能写进审计，后者不能。

4. **零新增依赖、零新增模型调用。**
   不需要 embedding 接口，不需要向量库，一个 JSONL 就够。
   对一个几人规模的运维台，向量库是纯粹的运维负担。

5. **天然是"人审过的"。**
   只有**人工批准且执行成功**的处置才会入库。每一条知识都有责任主体，
   而不是模型自己写进去的猜测——这一点比什么都重要。

6. **但"执行成功" ≠ "问题解决了"。**
   `apply_mutation` 返回 success 只说明 API Server 收了这次请求。
   内存上限改完 Pod 照样 OOM，那次调用**也是 success**。
   如果直接把它记成"成功案例"，下次检索出来就是一条**被人工批准过、
   却根本没用**的先例——记忆系统会自信地教错的东西，这比没有记忆更糟。

   所以案例要经过**回查**：执行完等一会儿，重新读一遍症状签名。
   症状全消失 → ``effective``（可以教）；还赖着不走 → ``ineffective``（不许教，
   而且要把**同症状同处置**的旧案例一并作废——既然这招现在不管用，
   那条更早的"成功"也不可信）。

反面也写清楚：如果将来要匹配的是"日志文本里描述的模糊现象"，
结构化匹配就不够了，那时候再上向量检索也不迟。**现在不需要。**
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# 症状词表。刻意用 ASCII 标识符而不是中文短语：
# 它们要参与匹配、要进审计、要能 grep，措辞稳定比好看重要。
TOKEN_LABEL = {
    "oom_killed": "容器被 OOM 杀过",
    "crashloop": "反复重启（CrashLoopBackOff）",
    "image_pull": "镜像拉不下来",
    "pending": "调度不上去（Pending）",
    "not_ready": "在跑但没就绪",
    "frequent_restarts": "频繁重启",
    "exit_137": "退出码 137（被 SIGKILL）",
    "no_endpoints": "Service 没有后端",
    "partial_endpoints": "部分后端未就绪",
    "targetport_mismatch": "Service 端口与容器端口对不上",
    "hpa_not_able_to_scale": "HPA 扩缩容失效",
    "scaled_to_zero": "被缩到 0 副本",
    "replicas_short": "副本数不足",
    "unhealthy": "整体不健康",
}


def _pod_tokens(pods: list[Any]) -> set[str]:
    out: set[str] = set()
    for p in pods:
        term = (getattr(p, "termination_reason", "") or "")
        if term.lower() == "oomkilled":
            out.add("oom_killed")
        if getattr(p, "last_exit_code", None) == 137:
            out.add("exit_137")
        restarts = int(getattr(p, "restarts", 0) or 0)
        if restarts >= 5:
            out.add("frequent_restarts")
        phase = getattr(p, "phase", "")
        if phase == "Pending":
            out.add("pending")
        elif phase == "Running" and not getattr(p, "ready", False):
            out.add("not_ready")
        # Pending 的 waiting reason 藏在 reason 字段里
        reason = (getattr(p, "reason", "") or "")
        joined = f"{term} {reason}".lower()
        if "crashloop" in joined:
            out.add("crashloop")
        if "imagepull" in joined or "errimagepull" in joined or "errimagenevers" in joined:
            out.add("image_pull")
    return out


def symptom_tokens(
    k8s: Any, namespace: str, kind: str, name: str, *, pods: list[Any] | None = None
) -> list[str]:
    """把一个工作负载**当前**的症状算成一组稳定的词。

    这是整个记忆机制的锚点：两次故障是不是"同一类"，
    在这里是**确定性的判断**，不是相似度猜测。
    """
    tokens: set[str] = set()
    try:
        if pods is None:
            wl = k8s.read_workload(namespace, kind, name)
            selector = (wl.spec.selector.match_labels or {}) if wl.spec.selector else {}
            sel = ",".join(f"{k}={v}" for k, v in selector.items())
            pods = k8s.list_pods(namespace, label_selector=sel)
        tokens |= _pod_tokens(pods)
    except Exception:  # noqa: BLE001
        pass

    # 副本层面
    try:
        wl = k8s.read_workload(namespace, kind, name)
        desired = int(getattr(wl.spec, "replicas", 0) or 0)
        ready = int(getattr(wl.status, "ready_replicas", 0) or 0)
        if desired == 0:
            tokens.add("scaled_to_zero")
        elif ready < desired:
            tokens.add("replicas_short")
    except Exception:  # noqa: BLE001
        pass

    # Service / Endpoints 层面：这类问题不会体现在 Pod 状态上
    try:
        for ep in k8s.list_endpoints(namespace):
            if ep.get("name") != name:
                continue
            r, nr = int(ep.get("ready") or 0), int(ep.get("not_ready") or 0)
            if r == 0 and nr == 0:
                tokens.add("no_endpoints")
            elif nr > 0:
                tokens.add("partial_endpoints")
    except Exception:  # noqa: BLE001
        pass

    # targetPort 与容器端口交叉比对
    try:
        wl = k8s.read_workload(namespace, kind, name)
        cports = {
            int(pt.container_port)
            for c in (wl.spec.template.spec.containers or [])
            for pt in (getattr(c, "ports", None) or [])
            if getattr(pt, "container_port", None)
        }
        for svc in k8s.list_services(namespace):
            if svc.get("name") != name:
                continue
            for p in svc.get("ports") or []:
                tp = str(p.get("target_port") or "")
                if tp.isdigit() and cports and int(tp) not in cports:
                    tokens.add("targetport_mismatch")
    except Exception:  # noqa: BLE001
        pass

    # HPA 层面
    try:
        for h in k8s.autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace).items:
            ref = getattr(h.spec.scale_target_ref, "name", "") or ""
            if ref != name and h.metadata.name != name:
                continue
            for c in ((h.status.conditions or []) if h.status else []):
                if c.type == "AbleToScale" and c.status == "False":
                    tokens.add("hpa_not_able_to_scale")
    except Exception:  # noqa: BLE001
        pass

    if tokens:
        tokens.add("unhealthy")
    return sorted(tokens)


def describe(tokens: list[str]) -> str:
    return "、".join(TOKEN_LABEL.get(t, t) for t in tokens) or "（无症状）"


def similarity(a: list[str], b: list[str]) -> float:
    """Jaccard 相似度。返回 0~1，用来排序和给用户看"匹配度"。"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# outcome 的三种取值：
#   applied      —— 已批准并执行，但还没回查过。**不参与检索**（结论未知）
#   effective     —— 回查确认症状消失了。这才是可以教给下一次的经验
#   ineffective   —— 回查发现症状还在。不许再教，并且作废同症状同处置的旧案例
OUTCOME_APPLIED = "applied"
OUTCOME_EFFECTIVE = "effective"
OUTCOME_INEFFECTIVE = "ineffective"


@dataclass
class Case:
    """一次处置记录。它是不是"经验"，取决于回查结果。"""

    signature: list[str]
    namespace: str
    workload: str
    kind: str
    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    operator: str = ""
    outcome: str = OUTCOME_APPLIED
    problems: list[str] = field(default_factory=list)
    ts: str = ""
    # 同一个 case 的多次"修订"（回查结果）靠 case_id 串起来，后写的覆盖先写的
    case_id: str = ""
    verified_at: str = ""
    evidence: str = ""   # 回查时读到的症状，留作"为什么判定它没用"的依据

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Case":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def render(self, score: float, current_workload: str = "") -> str:
        """给模型看的一行。目标不同的时候要说清楚，别让它照抄参数。"""
        same = (not current_workload) or self.workload == current_workload
        target_note = "" if same else f"（当时改的是 {self.workload}，这次是 {current_workload}，参数要相应调整）"
        age = (self.ts or "")[:19].replace("T", " ")
        return (
            f"- 匹配度 {int(score * 100)}% ｜ {age} ｜ 由 {self.operator or '未知'} 批准\n"
            f"  症状：{describe(self.signature)}{target_note}\n"
            f"  当时读到的现象：{' / '.join(self.problems[:3])}\n"
            f"  处置：{self.tool}({json.dumps(self.params, ensure_ascii=False)})\n"
            f"  结果：**回查确认症状消失**（{self.verified_at[:19].replace('T', ' ') or '已核' }）\n"
            f"  当时给的理由：{(self.rationale or '（未记录）')[:200]}"
        )


class CaseStore:
    """一个 JSONL 文件。

    形态是**事件日志**：同一个 ``case_id`` 可以有多条记录（先 applied，
    回查后追加 effective / ineffective），**最后一条为准**。
    这样既保持追加写的简单性，又能表达"这条经验被推翻了"。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._cache: list[Case] | None = None

    # ------------------------------------------------------------------ 写

    def append(self, case: Case) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(case.to_dict(), ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._cache = None

    # 兼容旧调用名
    record = append

    def update(self, case_id: str, **fields: Any) -> Case | None:
        """给已有案例追加一条修订（回查结果）。返回修订后的案例。"""
        cur = self.by_id(case_id)
        if cur is None:
            return None
        d = cur.to_dict()
        d.update(fields)
        new = Case.from_dict(d)
        self.append(new)
        return new

    def supersede(self, *, signature: list[str], tool: str,
                  reason: str = "") -> int:
        """把"同症状 + 同处置"的其它案例一并作废。

        逻辑：如果这招**现在没管用**，那么更早那次"成功"也不可信——
        很可能是当时环境不同，或者只是暂时缓解。留着它，模型下次还会
        引用一条已经被证伪的经验。
        """
        from .models import now_iso

        n = 0
        key = set(signature)
        for c in self.all():
            if c.tool != tool or c.outcome != OUTCOME_EFFECTIVE:
                continue
            if set(c.signature) != key:
                continue
            self.update(c.case_id,
                        outcome=OUTCOME_INEFFECTIVE,
                        verified_at=now_iso(),
                        evidence=f"被后续回查推翻：{reason}")
            n += 1
        return n

    # ------------------------------------------------------------------ 读

    def all(self) -> list[Case]:
        """最新状态。同 case_id 只保留最后一条。"""
        if self._cache is not None:
            return self._cache
        latest: dict[str, Case] = {}
        order: list[str] = []
        if self.path.is_file():
            with self.path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        c = Case.from_dict(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 坏行跳过，不要让一条烂数据毁掉整个库
                    # 老文件没有 case_id，按 时间+对象+动作 合成一个稳定 id
                    if not c.case_id:
                        c.case_id = f"legacy-{c.ts}-{c.workload}-{c.tool}-{lineno}"
                    if c.case_id not in latest:
                        order.append(c.case_id)
                    latest[c.case_id] = c
        self._cache = [latest[i] for i in order]
        return self._cache

    def by_id(self, case_id: str) -> Case | None:
        return next((c for c in self.all() if c.case_id == case_id), None)

    def pending_verification(self, *, min_age: float = 60, max_age: float = 6 * 3600,
                             now: float | None = None) -> list[Case]:
        """等待回查的案例。

        ``min_age`` 是**必须有的等待**：滚动更新要几十秒才收敛，
        执行完立刻回查只会看到"还没好"，把一次正确的处置误判成无效。
        """
        import time as _t

        now = now or _t.time()
        out = []
        for c in self.all():
            if c.outcome != OUTCOME_APPLIED or not c.signature:
                continue
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(c.ts.replace("Z", "+00:00")).timestamp()
            except Exception:  # noqa: BLE001
                continue
            age = now - ts
            if min_age <= age <= max_age:
                out.append(c)
        return out

    def search(self, tokens: list[str], *, limit: int = 5,
               min_score: float = 0.5) -> list[tuple[float, Case]]:
        """按症状签名找历史案例。

        **只返回回查确认有效的那些。** 阈值默认 0.5：低于这个值说明
        症状重叠得不够，硬塞给模型只会误导它。
        **宁可返回空，也不要给一条不相关的"经验"。**
        """
        if not tokens:
            return []
        scored = [
            (similarity(tokens, c.signature), c)
            for c in self.all()
            if c.outcome == OUTCOME_EFFECTIVE
        ]
        hits = [(s, c) for s, c in scored if s >= min_score]
        hits.sort(key=lambda x: (x[0], x[1].ts), reverse=True)
        return hits[:limit]

    def count_for(self, tokens: list[str], *, min_score: float = 0.5) -> int:
        return len(self.search(tokens, limit=1000, min_score=min_score))

    def stats(self) -> dict[str, Any]:
        cases = self.all()
        by: dict[str, int] = {}
        for c in cases:
            by[c.outcome] = by.get(c.outcome, 0) + 1
        return {
            "total": len(cases),
            "by_outcome": by,
            "teachable": by.get(OUTCOME_EFFECTIVE, 0),
            "workloads": len({c.workload for c in cases}),
            "tools": sorted({c.tool for c in cases}),
        }


def verify_applied(
    k8s: Any, store: CaseStore, *, min_age: float = 60, max_age: float = 6 * 3600
) -> list[tuple[Case, str]]:
    """回查"已执行但没验证"的案例：症状到底消失了没有。

    判定：重新算一遍那个工作负载的症状签名，和案发时比。

    - 交集为空            → ``effective``（症状全没了，这招管用）
    - 还重叠一半以上      → ``ineffective``（没解决），并把**同症状同处置**的
                            旧案例一并作废
    - 中间地带            → 保守起见也记 ``ineffective``
                            （只解决一半的招不该被当成经验教出去）

    返回 [(案例, 判定)]，供调用方回报。
    """
    from .models import now_iso

    out: list[tuple[Case, str]] = []
    for c in store.pending_verification(min_age=min_age, max_age=max_age):
        try:
            now_tokens = symptom_tokens(k8s, c.namespace, _kind_key(c.kind), c.workload)
        except Exception as exc:  # noqa: BLE001
            continue
        overlap = len(set(now_tokens) & set(c.signature))
        total = max(len(c.signature), 1)
        ratio = overlap / total

        if overlap == 0:
            store.update(c.case_id, outcome=OUTCOME_EFFECTIVE,
                         verified_at=now_iso(),
                         evidence=f"回查：症状已全部消失（当时 {describe(c.signature)}）")
            out.append((c, OUTCOME_EFFECTIVE))
        else:
            store.update(c.case_id, outcome=OUTCOME_INEFFECTIVE,
                         verified_at=now_iso(),
                         evidence=f"回查：仍有 {overlap}/{total} 个症状存在"
                                  f"（{describe(sorted(set(now_tokens) & set(c.signature)))}）")
            store.supersede(signature=c.signature, tool=c.tool,
                            reason=f"{c.workload} 上的同类处置未生效")
            out.append((c, OUTCOME_INEFFECTIVE))
    return out


def _kind_key(kind: str) -> str:
    """案例里存的是展示用的 Kind（Deployment/StatefulSet），查询要用小写。"""
    k = (kind or "deployment").lower()
    return {"deployment": "deployment", "statefulset": "statefulset",
            "daemonset": "daemonset"}.get(k, k)
    def stats(self) -> dict[str, Any]:
        cases = self.all()
        return {
            "total": len(cases),
            "workloads": len({c.workload for c in cases}),
            "tools": sorted({c.tool for c in cases}),
        }
