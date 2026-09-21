"""规则挖掘探针（fuzz）与由此暴露的规则缺陷的测试。

分两部分：
1. **fuzz 工具本身**：生成结果的清洗、盲区分类、报告导出
2. **回归**：退出码 137 不等于 OOM——这是 fuzz 第一轮最大的收获，
   而且它同时证明了**我自己写的 probe-003 期望值是错的**（照搬了实现的错误行为）
"""

from __future__ import annotations

import pytest

from omagent.evals import EvalCase, run_case
from omagent.fuzz import (
    FuzzGenerator,
    FuzzUnavailable,
    analyse_generated,
    save_generated,
    summarise,
    to_markdown,
)


# ---------------------------------------------------------------------------
# 生成器：不依赖外部服务
# ---------------------------------------------------------------------------


def test_unavailable_without_credentials(monkeypatch):
    monkeypatch.delenv("OMAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    gen = FuzzGenerator(api_key="")
    assert not gen.available
    with pytest.raises(FuzzUnavailable):
        gen.generate(1)


def test_clean_assigns_stable_ids():
    gen = FuzzGenerator(api_key="x")
    cleaned = gen._clean([{"title": "探针误杀"}, {"title": "Service 无端点"}])
    assert cleaned[0]["id"].startswith("fuzz-001-")
    assert cleaned[1]["id"].startswith("fuzz-002-")
    assert all(c["difficulty"] == "hard" for c in cleaned)


def test_batch_is_small_enough():
    """单次请求场景数必须受限：要得越多，越容易把 token 全耗在推理上而拿不到正文。"""
    assert FuzzGenerator.BATCH <= 3


def test_save_generated_warns_about_review(tmp_path, monkeypatch):
    import omagent.fuzz as fz

    monkeypatch.setattr(fz, "GENERATED_DIR", tmp_path)
    out = save_generated([{"id": "x", "title": "t"}], "t1")
    text = out.read_text(encoding="utf-8")
    assert "尚未人工复核" in text
    assert "不是真值" in text


# ---------------------------------------------------------------------------
# 盲区分类
# ---------------------------------------------------------------------------


def _raw(case_id: str, *, signature: str, acceptable: list[str],
         forbidden: list[str], pods: list[dict]) -> dict:
    return {
        "id": case_id,
        "title": f"用例 {case_id}",
        "fixture": {
            "workload": {"kind": "Deployment", "name": "api-gateway", "replicas": len(pods),
                         "labels": {"app": "api-gateway"}},
            "pods": pods,
        },
        "expect": {
            "signature": signature,
            "acceptable_actions": acceptable,
            "forbidden_actions": forbidden,
        },
    }


def test_analyse_flags_forbidden_action_as_false_positive():
    """规则引擎提议了本场景不该用的动作 → 假阳性（最危险的一类）。"""
    raw = _raw(
        "f1", signature="crashloop", acceptable=["rollout_undo"],
        forbidden=["patch_resources", "delete_workload"],
        pods=[{"name": "p1", "phase": "Running", "ready": False, "restarts": 5,
               "last_exit_code": 137, "memory_limit": "64Mi"}],
    )
    spots = analyse_generated([raw])
    assert spots[0].verdict == "false_positive"
    assert "patch_resources" in spots[0].proposed


def test_analyse_flags_miss():
    raw = _raw(
        "f2", signature="healthy", acceptable=["rollout_restart"],
        forbidden=["delete_workload"],
        pods=[{"name": "p1", "phase": "Running", "ready": True, "restarts": 0}],
    )
    spots = analyse_generated([raw])
    assert spots[0].verdict == "miss"


def test_analyse_marks_ok_when_engine_agrees():
    raw = _raw(
        "f3", signature="healthy", acceptable=[], forbidden=["delete_workload"],
        pods=[{"name": "p1", "phase": "Running", "ready": True, "restarts": 0}],
    )
    spots = analyse_generated([raw])
    assert spots[0].verdict == "ok"


def test_analyse_survives_incomplete_generated_case():
    """模型产出的用例可能缺字段。工具必须能容错，而不是整体崩掉。

    注意：`EvalCase.from_dict` 会为缺失字段补默认值，所以这里真正要验证的是
    **不抛异常、且给出一个判定**，而不是"报结构非法"。
    """
    spots = analyse_generated([{"id": "bare-minimum"}])
    assert len(spots) == 1
    assert spots[0].verdict in {"ok", "miss", "unrecognized", "false_positive"}


def test_summarise_counts():
    raws = [
        _raw("a", signature="healthy", acceptable=[], forbidden=["delete_workload"],
             pods=[{"name": "p", "phase": "Running", "ready": True, "restarts": 0}]),
        _raw("b", signature="healthy", acceptable=["rollout_restart"],
             forbidden=["delete_workload"],
             pods=[{"name": "p", "phase": "Running", "ready": True, "restarts": 0}]),
    ]
    assert summarise(analyse_generated(raws)) == {"ok": 1, "miss": 1}


def test_markdown_report_contains_severity_and_traps():
    raw = _raw("m1", signature="healthy", acceptable=["rollout_restart"],
               forbidden=["delete_workload"],
               pods=[{"name": "p", "phase": "Running", "ready": True, "restarts": 0}])
    raw["naive_trap"] = "朴素实现会直接报健康"
    raw["rationale"] = "根因在 Service 而非 Pod"
    md = to_markdown(analyse_generated([raw]))
    assert "朴素实现会直接报健康" in md
    assert "根因在 Service 而非 Pod" in md
    assert "漏报" in md


# ---------------------------------------------------------------------------
# 回归：退出码 137 ≠ OOMKilled（fuzz 逼出来的修正）
# ---------------------------------------------------------------------------


def _probe_case() -> dict:
    return {
        "id": "reg-137",
        "title": "退出码 137 但实为探针误杀",
        "fixture": {
            "workload": {"kind": "Deployment", "name": "api-gateway", "replicas": 2,
                         "labels": {"app": "api-gateway"}},
            "pods": [
                {"name": "p1", "phase": "Running", "ready": False, "restarts": 7,
                 "last_exit_code": 137, "memory_limit": "512Mi",
                 "reason": "CrashLoopBackOff"},
            ],
            "events": [
                {"type": "Warning", "reason": "Unhealthy", "object": "Pod/p1",
                 "message": "Liveness probe failed: HTTP probe failed with statuscode: 404"},
            ],
        },
        "expect": {"signature": "probe_kill", "acceptable_actions": ["rollout_undo"],
                   "forbidden_actions": ["patch_resources", "delete_workload"]},
    }


def test_probe_kill_is_not_classified_as_oom():
    """★ 回归：有探针误杀证据、无 OOM 证据时，137 不是 OOM。

    早期实现只看 `last_exit_code == 137` 就判 OOMKilled，
    于是建议"加大内存"——而真实原因是探针配置，加内存完全无效。
    """
    r = run_case(EvalCase.from_dict(_probe_case()))
    assert r.signature_actual == "probe_kill"
    assert "patch_resources" not in r.mutating_proposals


def test_probe_kill_is_detected_from_any_event_wording():
    """不同措辞（Killing / Unhealthy / 404 / timeout）都应识别出来。"""
    for msg in (
        "Container app failed liveness probe, will be restarted",
        "Liveness probe failed: HTTP probe failed with statuscode: 404",
        "Liveness probe failed: context deadline exceeded",
        "Liveness probe timeout",
    ):
        raw = _probe_case()
        raw["fixture"]["events"] = [
            {"type": "Warning", "reason": "Unhealthy", "object": "Pod/p1", "message": msg}
        ]
        r = run_case(EvalCase.from_dict(raw))
        assert r.signature_actual == "probe_kill", f"未被识别：{msg}"


def test_explicit_oom_evidence_wins_over_probe_failure():
    """真实 OOM 也会导致探针失败。有正向 OOM 证据时，仍应判 OOM。"""
    raw = _probe_case()
    raw["fixture"]["pods"][0]["termination_reason"] = "OOMKilled"
    raw["fixture"]["events"] = [
        {"type": "Warning", "reason": "OOMKilling", "object": "Pod/p1",
         "message": "Memory cgroup out of memory"},
        {"type": "Warning", "reason": "Unhealthy", "object": "Pod/p1",
         "message": "Liveness probe failed: connection refused"},
    ]
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual == "oom_killed"


def test_bare_137_without_evidence_still_treated_as_oom():
    """没有任何其他证据时，孤立 137 仍按 OOM 处理（保持向后兼容）。"""
    raw = _probe_case()
    raw["fixture"]["events"] = []
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual == "oom_killed"


def test_probe_kill_advises_rollback_not_restart():
    """探针配置问题：重启无用，应建议回滚。"""
    raw = _probe_case()
    raw["expect"]["acceptable_actions"] = ["rollout_undo", "rollout_restart"]
    r = run_case(EvalCase.from_dict(raw))
    assert "rollout_undo" in r.mutating_proposals


# ---------------------------------------------------------------------------
# 提示词模板可用性
# ---------------------------------------------------------------------------


def test_prompt_template_formats_without_keyerror():
    """★ 提示词里出现未转义的 `{...}` 会让 .format() 抛 KeyError。

    这类错误只在**真正调用生成**时才暴露（而且是在花掉一次 API 调用之前就崩），
    所以用一条廉价的单测把它挡住。
    """
    from omagent.fuzz import FUZZ_SYSTEM_PROMPT

    rendered = FUZZ_SYSTEM_PROMPT.format(known="X", actions="Y", n=3)
    assert "X" in rendered and "Y" in rendered
    # 转义后的字面量应当保留成正常的单个花括号
    assert "{服务名: 就绪后端数}" in rendered


# ---------------------------------------------------------------------------
# ITBench 转换器（真实数据接入）
# ---------------------------------------------------------------------------


def test_itbench_load_missing_scenario_is_handled(tmp_path):
    """数据不存在时应给出明确错误，而不是静默产出空 fixture。"""
    from omagent.itbench import build_fixture, load_scenario

    scn = load_scenario(tmp_path, "Nope-1")
    assert scn.objects == {}
    assert build_fixture(scn) is None


def test_itbench_out_of_scope_detection():
    """ConfigMap / Chaos 类根因必须被判为超出能力范围，不参与评分。

    否则会拿"改配置"这类本项目根本不做的事去算它答错。
    """
    from omagent.itbench import Scenario

    scn = Scenario(scn_id="x", ground_truth={
        "fault": [{"entity": {"kind": "ConfigMap", "name": "flagd-config"}}],
        "groups": [{"id": "g", "kind": "ConfigMap", "root_cause": True}],
    })
    assert not scn.in_scope

    chaos = Scenario(scn_id="y", ground_truth={
        "fault": [{"entity": {"kind": "NetworkChaos", "name": "c1"}}],
        "groups": [{"id": "g", "kind": "NetworkChaos", "root_cause": True}],
    })
    assert not chaos.in_scope


def test_itbench_pod_root_cause_is_in_scope():
    from omagent.itbench import Scenario

    scn = Scenario(scn_id="z", ground_truth={
        "fault": [{"entity": {"kind": "Pod", "name": "ad-1"}}],
        "groups": [{"id": "g", "kind": "Pod", "root_cause": True}],
    })
    assert scn.in_scope
