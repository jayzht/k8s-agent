"""知识沉淀：故障四元组的抽取、存储与相似检索。

为什么这是护城河
----------------
一个只会"每次从零推理"的 Agent，能力上限就是模型的能力上限。
而一个能把每次处置沉淀成**可复用经验**的 Agent，会随使用变得越来越快、越来越准：

    症状 → 根因 → 处置 → 结果

这是开源基座给不了的东西，也是本项目的差异化资产（PRD 第 2 节 · 张力 3）。

设计要点
--------
1. **追加写 JSONL**，与审计日志同构但职责不同：审计是"谁做了什么"（合规），
   知识是"这类问题怎么解"（经验）。二者不合并，避免一方污染另一方。
2. **不存原始日志**，只存提取出的关键词。既省空间，也降低敏感信息落库风险。
3. **相似检索不用外部依赖**：签名精确匹配 + 关键词 Jaccard，足够支撑"第 N 次更快"。
4. **知识只是建议，不改变安全边界**：检索结果进入诊断叙述与确认卡片，
   但**不会**自动执行任何动作，也不会绕过审批门禁。
"""

from __future__ import annotations

import hashlib
import os
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWLEDGE_PATH = ROOT / "var" / "knowledge.jsonl"

# 关键词提取时忽略的噪声词（K8s 领域高频但无区分度）
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "of", "to",
    "in", "on", "for", "with", "at", "by", "from", "as", "it", "this", "that",
    "pod", "pods", "container", "containers", "container=", "ns", "namespace",
    "deployment", "statefulset", "daemonset", "replicaset", "kubectl", "get",
    "describe", "logs", "error", "warn", "warning", "info", "failed", "failure",
    "true", "false", "none", "null", "http", "https", "ready",
}

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-\.]{2,}")


def extract_keywords(text: str, limit: int = 24) -> list[str]:
    """从证据文本中提取有区分度的关键词。

    刻意做得简单可解释：出现频次高、长度足够、不在停用词表里的 token。
    复杂的 NLP 在这里收益有限，而可解释性对运维场景很重要。
    """
    counts: dict[str, int] = {}
    for raw in _TOKEN_RE.findall(text or ""):
        tok = raw.lower().strip(".-_")
        if len(tok) < 3 or tok in STOPWORDS or tok.isdigit():
            continue
        counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:limit]]


@dataclass
class KnowledgeEntry:
    """一条「症状 → 根因 → 处置 → 结果」四元组。"""

    entry_id: str = field(default_factory=lambda: f"kb-{uuid.uuid4().hex[:10]}")
    ts: str = ""
    namespace: str = ""
    workload: str = ""
    kind: str = ""
    signature: str = ""
    symptom: str = ""
    root_cause: str = ""
    treatment: str = ""
    treatment_tier: str = ""
    outcome: str = ""          # success | failed | refused | cancelled | no_action
    outcome_note: str = ""
    evidence_keywords: list[str] = field(default_factory=list)
    operator: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())

    @property
    def resolved(self) -> bool:
        return self.outcome == "success"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        mark = {"success": "✅", "failed": "❌", "refused": "⛔", "cancelled": "🚫"}.get(
            self.outcome, "•"
        )
        return (
            f"{mark} [{self.ts[:19]}] {self.signature} @ {self.workload}\n"
            f"    根因：{self.root_cause[:110]}\n"
            f"    处置：{self.treatment or '（未执行动作）'} → {self.outcome}"
        )


class ConfigMapHistory:
    """ConfigMap 版本快照。

    K8s **原生不保留 ConfigMap 历史**，所以"回滚配置"这件事必须自己记。
    做法：每次读到 ConfigMap 就记一份（键值 + 内容哈希），只保留最近 N 个版本。
    这样配置改坏时才有"上一个版本"可回滚。
    """

    KEEP = 5

    def __init__(self, path: str | Path | None = None):
        # 路径可用环境变量覆盖，便于测试隔离与多环境部署
        self.path = Path(
            path or os.environ.get("OMAGENT_CM_HISTORY")
            or (ROOT / "var" / "configmap-history.json")
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _hash(data: dict[str, str]) -> str:
        blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def record(self, namespace: str, name: str, data: dict[str, str]) -> str:
        """记录一次快照，返回内容哈希。内容未变则只更新时间。"""
        store = self._load()
        key = f"{namespace}/{name}"
        digest = self._hash(data)
        versions = store.get(key, [])
        if versions and versions[-1]["hash"] == digest:
            versions[-1]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        else:
            versions.append({
                "hash": digest, "data": data,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            })
        store[key] = versions[-self.KEEP :]
        self.path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        return digest

    def previous(self, namespace: str, name: str) -> dict[str, str] | None:
        """返回**上一个不同**的版本（用于回滚）；没有则 None。"""
        versions = self._load().get(f"{namespace}/{name}", [])
        if len(versions) < 2:
            return None
        return dict(versions[-2]["data"])

    def current(self, namespace: str, name: str) -> dict[str, str] | None:
        versions = self._load().get(f"{namespace}/{name}", [])
        return dict(versions[-1]["data"]) if versions else None

    def version_count(self, namespace: str, name: str) -> int:
        return len(self._load().get(f"{namespace}/{name}", []))


class KnowledgeStore:
    """追加写知识库 + 相似检索。"""

    def __init__(self, path: str | Path = DEFAULT_KNOWLEDGE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 写入

    def add(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
        return entry

    def record_from_diagnosis(
        self,
        *,
        namespace: str,
        workload: str,
        kind: str,
        signature: str,
        conclusion: str,
        evidence_text: str,
        treatment: str = "",
        treatment_tier: str = "",
        outcome: str = "no_action",
        outcome_note: str = "",
        operator: str = "",
    ) -> KnowledgeEntry:
        """从一次诊断（可含处置）抽取一条四元组。

        症状 = 故障特征 + 证据关键词；其余字段直接来自诊断与执行结果。
        """
        entry = KnowledgeEntry(
            namespace=namespace,
            workload=workload,
            kind=kind,
            signature=signature,
            symptom=f"{signature}｜{conclusion[:120]}",
            root_cause=conclusion,
            treatment=treatment,
            treatment_tier=treatment_tier,
            outcome=outcome,
            outcome_note=outcome_note,
            evidence_keywords=extract_keywords(evidence_text),
            operator=operator,
        )
        return self.add(entry)

    # ------------------------------------------------------------------ 读取

    def all(self) -> list[KnowledgeEntry]:
        if not self.path.exists():
            return []
        out: list[KnowledgeEntry] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(KnowledgeEntry(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        return out

    # ------------------------------------------------------------------ 检索

    def similar(
        self,
        signature: str,
        keywords: list[str] | None = None,
        workload: str = "",
        limit: int = 5,
    ) -> list[tuple[float, KnowledgeEntry]]:
        """按"签名精确匹配 + 关键词重合度 + 同工作负载"给历史条目打分。"""
        kws = set(keywords or [])
        scored: list[tuple[float, KnowledgeEntry]] = []
        for entry in self.all():
            score = 0.0
            if signature and entry.signature == signature:
                score += 0.6
            if kws:
                inter = kws & set(entry.evidence_keywords)
                union = kws | set(entry.evidence_keywords)
                if union:
                    score += 0.3 * (len(inter) / len(union))
            if workload and entry.workload == workload:
                score += 0.1
            if score > 0:
                scored.append((round(score, 4), entry))
        scored.sort(key=lambda kv: (-kv[0], kv[1].ts), reverse=False)
        return scored[:limit]

    # ------------------------------------------------------------------ 洞察

    def hint_for(self, signature: str, keywords: list[str] | None = None,
                 workload: str = "") -> str:
        """生成一句"历史上遇到过吗"的提示，用于诊断结论与确认卡片。"""
        hits = self.similar(signature, keywords, workload, limit=10)
        if not hits:
            return ""
        same_sig = [e for _, e in hits if e.signature == signature]
        if not same_sig:
            return ""

        # 统计历史上真正解决过这个问题的处置
        treatments: dict[str, list[str]] = {}
        for e in same_sig:
            if e.treatment:
                treatments.setdefault(e.treatment, []).append(e.outcome)

        parts = [f"历史上有 {len(same_sig)} 次同类（{signature}）记录"]
        if treatments:
            ranked = sorted(
                treatments.items(),
                key=lambda kv: (-kv[1].count("success"), -len(kv[1])),
            )
            best, results = ranked[0]
            ok = results.count("success")
            # treatment 存的是 "tool {params}"，提示里只展示动作名——
            # 把整个参数字典塞进一句话会非常难读（真实界面上发现的问题）。
            parts.append(
                f"最常用处置是 {best.split(' ')[0]}（{ok}/{len(results)} 次成功）"
            )
        else:
            parts.append("但历史上均未执行变更动作")
        if workload and any(e.workload == workload for e in same_sig):
            n = sum(1 for e in same_sig if e.workload == workload)
            parts.append(f"其中 {n} 次发生在该工作负载上")
        return "；".join(parts) + "。"

    def stats(self) -> dict[str, Any]:
        entries = self.all()
        if not entries:
            return {"total": 0}
        by_sig: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        by_treatment: dict[str, int] = {}
        for e in entries:
            by_sig[e.signature] = by_sig.get(e.signature, 0) + 1
            by_outcome[e.outcome] = by_outcome.get(e.outcome, 0) + 1
            if e.treatment:
                by_treatment[e.treatment] = by_treatment.get(e.treatment, 0) + 1
        return {
            "total": len(entries),
            "by_signature": dict(sorted(by_sig.items(), key=lambda kv: -kv[1])),
            "by_outcome": by_outcome,
            "by_treatment": dict(sorted(by_treatment.items(), key=lambda kv: -kv[1])),
            "workloads": len({e.workload for e in entries}),
        }
