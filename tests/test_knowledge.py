"""知识沉淀（故障四元组）的测试。

覆盖两件事：
1. **抽取正确**：症状/根因/处置/结果四元组能从一次诊断里被正确落库
2. **检索有用**：同类故障能被检索出来，并生成"历史上怎么处理的"提示
"""

from __future__ import annotations

import json

import pytest

from omagent.knowledge import (
    STOPWORDS,
    KnowledgeEntry,
    KnowledgeStore,
    extract_keywords,
)

# ---------------------------------------------------------------------------
# 关键词抽取
# ---------------------------------------------------------------------------


def test_extract_keywords_filters_stopwords_and_noise():
    text = "Pod api-gateway-7d9f phase=Running OOMKilled exit code 137 the is a"
    kws = extract_keywords(text)
    assert "oomkilled" in kws
    assert "api-gateway-7d9f" in kws
    assert "137" not in kws          # 纯数字被过滤
    assert "the" not in kws
    assert "pod" not in kws          # 领域噪声词被过滤


def test_extract_keywords_is_deterministic_and_bounded():
    text = " ".join(f"token{i}" for i in range(200))
    a = extract_keywords(text, limit=10)
    b = extract_keywords(text, limit=10)
    assert a == b
    assert len(a) == 10


def test_extract_keywords_handles_empty():
    assert extract_keywords("") == []
    assert extract_keywords(None) == []  # type: ignore[arg-type]


def test_stopwords_do_not_include_meaningful_terms():
    """停用词表不能误伤有诊断意义的词。"""
    for meaningful in ("crashloopbackoff", "oomkilled", "unschedulable", "no", "such", "host"):
        assert meaningful not in STOPWORDS


# ---------------------------------------------------------------------------
# 存储与检索
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "kb.jsonl")


def _add(store, signature="oom_killed", workload="api-gateway", treatment="rollout_restart",
         outcome="success", evidence="OOMKilled exit 137 memory limit 64Mi"):
    return store.record_from_diagnosis(
        namespace="demo",
        workload=workload,
        kind="deployment",
        signature=signature,
        conclusion=f"{workload} 因内存超限反复重启",
        evidence_text=evidence,
        treatment=treatment,
        treatment_tier="T1",
        outcome=outcome,
        operator="tester",
    )


def test_record_and_read_back(store):
    _add(store)
    entries = store.all()
    assert len(entries) == 1
    e = entries[0]
    assert e.signature == "oom_killed"
    assert e.workload == "api-gateway"
    assert e.treatment == "rollout_restart"
    assert e.outcome == "success"
    assert e.resolved
    assert e.evidence_keywords


def test_append_only_never_loses_history(store):
    for i in range(5):
        _add(store, workload=f"app-{i}")
    assert len(store.all()) == 5
    # 文件是追加写的 JSONL，一行一条
    lines = [ln for ln in store.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 5
    for ln in lines:
        json.loads(ln)  # 每行都是合法 JSON


def test_similar_matches_same_signature(store):
    _add(store, signature="oom_killed")
    _add(store, signature="image_pull", treatment="rollout_undo")
    hits = store.similar("oom_killed", [], "api-gateway")
    assert hits
    assert hits[0][1].signature == "oom_killed"


def test_similar_ranks_workload_match_higher(store):
    _add(store, workload="api-gateway")
    _add(store, workload="other-app")
    hits = store.similar("oom_killed", [], "api-gateway", limit=5)
    assert hits[0][1].workload == "api-gateway"


def test_similar_returns_empty_for_unknown(store):
    _add(store)
    assert store.similar("node_failure", [], "") == []


def test_hint_mentions_history_and_best_treatment(store):
    _add(store, treatment="rollout_restart", outcome="success")
    _add(store, treatment="rollout_restart", outcome="success")
    _add(store, treatment="patch_resources", outcome="failed")
    hint = store.hint_for("oom_killed", [], "api-gateway")
    assert "3 次同类" in hint
    assert "rollout_restart" in hint
    assert "2/2 次成功" in hint


def test_hint_empty_when_no_history(store):
    assert store.hint_for("oom_killed", [], "api-gateway") == ""


def test_hint_notes_when_history_had_no_action(store):
    _add(store, treatment="", outcome="no_action")
    hint = store.hint_for("oom_killed", [], "")
    assert "未执行变更动作" in hint


def test_stats_aggregates(store):
    _add(store, signature="oom_killed", outcome="success")
    _add(store, signature="oom_killed", outcome="failed")
    _add(store, signature="crashloop", outcome="success")
    st = store.stats()
    assert st["total"] == 3
    assert st["by_signature"]["oom_killed"] == 2
    assert st["by_outcome"]["success"] == 2


def test_stats_on_empty_store(store):
    assert store.stats() == {"total": 0}


def test_corrupt_line_is_skipped_not_fatal(store):
    _add(store)
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write("{ this is not valid json\n")
    _add(store)
    assert len(store.all()) == 2  # 坏行被跳过，好的记录仍可读


def test_entry_defaults_timestamp():
    e = KnowledgeEntry(signature="x")
    assert e.ts  # 自动填充
    assert not e.resolved


def test_render_is_readable(store):
    _add(store)
    text = store.all()[0].render()
    assert "oom_killed" in text
    assert "✅" in text
