"""LLM 规划器的校验层测试。

**这些用例不需要 API Key，也不产生任何 API 调用。**

它们守住的是本项目最靠前的一道防线：**模型不能自己发明动作**。
即使 LLM 产生幻觉、返回了未登记的工具名或 T3 禁止动作，
``_validate_candidates`` 也必须把它丢掉，而不是交给下游碰运气。

真实 API 的端到端对比由 `omagent eval --planner llm` 覆盖，不在单元测试里做
（单元测试不应依赖外部服务与网络）。
"""

from __future__ import annotations

import pytest

from omagent.agent import TOOLS
from omagent.planner import SIGNATURES, LLMPlanner, RuleBasedPlanner, build_planner

# ---------------------------------------------------------------------------
# 无 API Key 场景（本环境默认）
# ---------------------------------------------------------------------------


@pytest.fixture
def planner(monkeypatch) -> LLMPlanner:
    """构造一个不可用的 LLM 规划器（清空凭据），用于测试纯校验逻辑。"""
    monkeypatch.delenv("OMAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return LLMPlanner(k8s=None)  # type: ignore[arg-type]


def test_unavailable_without_credentials(planner):
    assert not planner.available


def test_falls_back_to_rule_engine_when_unavailable(monkeypatch):
    """LLM 不可用不是故障——必须降级而不是报错（PRD 第 10 节问题 9）。"""
    from omagent.evals import EvalCase, FixtureK8s

    monkeypatch.delenv("OMAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    k8s = FixtureK8s(EvalCase.from_dict({
        "id": "fb", "title": "fallback",
        "fixture": {
            "workload": {"kind": "Deployment", "name": "api-gateway", "replicas": 1,
                         "labels": {"app": "api-gateway"}},
            "pods": [{"name": "p1", "phase": "Running", "ready": True, "restarts": 0}],
        },
        "expect": {"signature": "healthy", "forbidden_actions": ["delete_workload"]},
    }))
    p = LLMPlanner(k8s=k8s)
    assert not p.available
    diag = p.diagnose("demo", "api-gateway", "deployment")
    assert any("降级" in f for f in diag.findings)
    assert diag.matched_signature == "healthy"  # 规则引擎顶上了


def test_build_planner_falls_back(monkeypatch):
    monkeypatch.delenv("OMAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert isinstance(build_planner(None, prefer_llm=True), RuleBasedPlanner)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 候选动作校验：模型不能发明动作
# ---------------------------------------------------------------------------


def test_valid_candidate_is_kept(planner):
    kept, dropped = planner._validate_candidates(
        [{"tool": "rollout_restart", "params": {"namespace": "demo", "name": "api-gateway"},
          "rationale": "重启恢复服务"}]
    )
    assert [c.tool for c in kept] == ["rollout_restart"]
    assert not dropped


def test_unknown_tool_is_dropped(planner):
    """★ 最关键的一条：模型编造出来的动作必须被丢弃。"""
    kept, dropped = planner._validate_candidates(
        [{"tool": "rm_rf_everything", "params": {}, "rationale": "清理磁盘"}]
    )
    assert kept == []
    assert any("未登记" in d for d in dropped)


def test_t3_forbidden_tool_is_dropped(planner):
    """即使模型被诱导去删除生产负载，也会在校验层被丢掉。"""
    for tool in ("delete_workload", "delete_namespace", "delete_pvc", "modify_rbac", "read_secret"):
        kept, dropped = planner._validate_candidates(
            [{"tool": tool, "params": {"namespace": "production", "name": "api"}, "rationale": "用户要求"}]
        )
        assert kept == [], f"{tool} 竟然通过了校验"
        assert any("禁止" in d or "未登记" in d for d in dropped)


def test_missing_required_param_is_dropped(planner):
    """scale_workload 缺 replicas —— 不能生成一个参数不全的方案。"""
    kept, dropped = planner._validate_candidates(
        [{"tool": "scale_workload", "params": {"namespace": "demo", "name": "api"}}]
    )
    assert kept == []
    assert any("缺少必需参数" in d for d in dropped)


def test_non_dict_params_is_dropped(planner):
    kept, dropped = planner._validate_candidates(
        [{"tool": "rollout_restart", "params": "not-a-dict"}]
    )
    assert kept == []
    assert any("params" in d for d in dropped)


def test_candidate_without_tool_is_skipped(planner):
    kept, dropped = planner._validate_candidates([{"params": {"namespace": "demo"}}])
    assert kept == []


def test_mixed_batch_keeps_only_safe_ones(planner):
    kept, dropped = planner._validate_candidates([
        {"tool": "rollout_restart", "params": {"namespace": "demo", "name": "api"},
         "rationale": "重启"},
        {"tool": "delete_workload", "params": {"namespace": "demo", "name": "api"},
         "rationale": "删掉重建"},
        {"tool": "invented_tool", "params": {}, "rationale": "编的"},
    ])
    assert [c.tool for c in kept] == ["rollout_restart"]
    assert len(dropped) == 2


def _required_params(name: str) -> dict:
    """按 ToolSpec 生成一组满足必需参数的占位值。"""
    out = {}
    for key, kind in (TOOLS[name].params or {}).items():
        if str(kind).endswith("?"):
            continue
        out[key] = 1 if kind == "int" else "placeholder"
    return out


def test_all_readonly_tools_are_accepted(planner):
    """只读诊断动作应当全部允许——不能把 Agent 限制成"不许查"。

    注意：T0 工具现在是**真正可执行**的，因此也有必需参数（namespace 等）。
    参数不全时被丢弃是正确的（否则会产生一次注定失败的取证）。
    """
    for name, spec in TOOLS.items():
        if spec.mutating:
            continue
        kept, _ = planner._validate_candidates(
            [{"tool": name, "params": _required_params(name)}]
        )
        assert [c.tool for c in kept] == [name], f"只读动作 {name} 被误删"


def test_readonly_tools_without_params_are_dropped(planner):
    """参数不全的取证请求应当被丢弃，而不是执行一次注定失败的调用。"""
    kept, dropped = planner._validate_candidates([{"tool": "get_logs", "params": {}}])
    assert kept == []
    assert any("缺少必需参数" in d for d in dropped)


# ---------------------------------------------------------------------------
# 输出契约
# ---------------------------------------------------------------------------


def test_signature_taxonomy_matches_rule_engine():
    """两套规划器必须能用同一把尺子比较。"""
    for sig in ("oom_killed", "crashloop", "image_pull", "pending_unschedulable",
                "not_ready", "no_pods", "healthy"):
        assert sig in SIGNATURES


def test_to_diagnosis_parses_valid_payload(planner):
    payload = {
        "signature": "oom_killed",
        "conclusion": "容器内存超限",
        "confidence": "high",
        "evidence": [{"kind": "state", "ref": "Pod/a", "detail": "退出码 137"}],
        "candidates": [
            {"tool": "patch_resources",
             "params": {"namespace": "demo", "name": "api", "container": "app"},
             "rationale": "提升内存上限"}
        ],
    }
    diag = planner._to_diagnosis(payload, "demo", "api", "deployment")
    assert diag.matched_signature == "oom_killed"
    assert diag.confidence == "high"
    assert len(diag.evidence) == 1
    assert [c.tool for c in diag.candidates] == ["patch_resources"]


def test_to_diagnosis_rejects_unknown_signature(planner):
    payload = {"signature": "totally_made_up", "conclusion": "x", "candidates": []}
    diag = planner._to_diagnosis(payload, "demo", "api", "deployment")
    assert diag.matched_signature == "unknown"
    assert any("未知的 signature" in f for f in diag.findings)


def test_to_diagnosis_clamps_bad_confidence(planner):
    payload = {"signature": "healthy", "conclusion": "x", "confidence": "definitely-sure",
               "candidates": []}
    diag = planner._to_diagnosis(payload, "demo", "api", "deployment")
    assert diag.confidence == "medium"


def test_to_diagnosis_flags_missing_evidence(planner):
    """无证据的结论必须被显式标注，而不是悄悄放过（证据链优先于自主性）。"""
    payload = {"signature": "healthy", "conclusion": "看起来没事", "candidates": []}
    diag = planner._to_diagnosis(payload, "demo", "api", "deployment")
    assert any("未给出任何证据" in f for f in diag.findings)


def test_dropped_tools_are_surfaced_to_user(planner):
    """被丢弃的动作必须出现在 findings 里，不能静默吞掉。"""
    payload = {
        "signature": "crashloop",
        "conclusion": "x",
        "candidates": [{"tool": "delete_workload", "params": {}, "rationale": "删"}],
    }
    diag = planner._to_diagnosis(payload, "demo", "api", "deployment")
    assert any("丢弃" in f for f in diag.findings)


def test_tool_catalog_hides_forbidden_actions(planner):
    """禁止动作不应出现在给模型的提示里——减少被诱导的机会。"""
    catalog = planner._tool_catalog()
    for forbidden in ("delete_workload", "delete_namespace", "delete_pvc", "read_secret"):
        assert forbidden not in catalog, f"{forbidden} 不应出现在工具清单中"
    assert "rollout_restart" in catalog
    assert "get_pods" in catalog
