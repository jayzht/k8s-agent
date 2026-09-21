"""自然语言意图层的测试。

最核心的一条：**意图解析的产物必须能在集群里真实找到**。
这样即使模型胡编一个工作负载名，也不会把下游诊断引到错误目标上。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omagent.intent import Intent, IntentParser, IntentUnresolved, _rule_parse, list_workloads
from omagent.policy import Policy


# ---------------------------------------------------------------------------
# 替身：一个只有工作负载列表的假集群
# ---------------------------------------------------------------------------


class FakeApps:
    def __init__(self, deployments: dict[str, list[str]], statefulsets: dict[str, list[str]]):
        self._d = deployments
        self._s = statefulsets

    def list_namespaced_deployment(self, ns):
        return SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in self._d.get(ns, [])]
        )

    def list_namespaced_stateful_set(self, ns):
        return SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in self._s.get(ns, [])]
        )


class FakeK8s:
    def __init__(self, deployments=None, statefulsets=None):
        # 注意：不能用 `or` 兜底——传空字典 {} 是合法用法（表示"没有该类资源"），
        # 但它是 falsy，会被 `or` 悄悄替换成默认值。
        self.apps = FakeApps(
            {"demo": ["api-gateway", "billing-core"]} if deployments is None else deployments,
            {"demo": ["session-store"]} if statefulsets is None else statefulsets,
        )


@pytest.fixture
def parser(tmp_path, monkeypatch) -> IntentParser:
    """不配置 LLM 凭据 → 走规则兜底，测试不依赖外部服务。"""
    monkeypatch.delenv("OMAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return IntentParser(FakeK8s(), Policy(state_path=tmp_path / "s.json"))


# ---------------------------------------------------------------------------
# 工作负载枚举
# ---------------------------------------------------------------------------


def test_list_workloads_covers_deployments_and_statefulsets():
    wls = list_workloads(FakeK8s(), ["demo"])
    names = {w["name"] for w in wls}
    assert names == {"api-gateway", "billing-core", "session-store"}
    kinds = {w["kind"] for w in wls}
    assert kinds == {"deployment", "statefulset"}


def test_list_workloads_survives_missing_namespace():
    """某个命名空间不存在不应导致整体失败。"""
    k8s = FakeK8s(deployments={"demo": ["a"]}, statefulsets={})
    wls = list_workloads(k8s, ["demo", "nope"])
    assert [w["name"] for w in wls] == ["a"]


# ---------------------------------------------------------------------------
# 规则兜底
# ---------------------------------------------------------------------------


def test_rule_parse_finds_workload_name():
    avail = [{"namespace": "demo", "kind": "deployment", "name": "api-gateway"}]
    intent = _rule_parse("api-gateway 一直重启", avail)
    assert intent is not None
    assert intent.workload == "api-gateway"
    assert intent.source == "rule"


def test_rule_parse_normalises_separators():
    """'api gateway' 应能匹配到 'api-gateway'。"""
    avail = [{"namespace": "demo", "kind": "deployment", "name": "api-gateway"}]
    intent = _rule_parse("API Gateway 有问题", avail)
    assert intent is not None and intent.workload == "api-gateway"


def test_rule_parse_prefers_longer_name():
    avail = [
        {"namespace": "demo", "kind": "deployment", "name": "gateway"},
        {"namespace": "demo", "kind": "deployment", "name": "api-gateway"},
    ]
    intent = _rule_parse("api-gateway 挂了", avail)
    assert intent.workload == "api-gateway"


def test_rule_parse_returns_none_when_no_match():
    avail = [{"namespace": "demo", "kind": "deployment", "name": "api-gateway"}]
    assert _rule_parse("订单服务挂了", avail) is None


def test_parser_falls_back_to_rule_without_llm(parser):
    intent = parser.parse("billing-core 好像有问题")
    assert intent.source == "rule"
    assert intent.workload == "billing-core"
    assert intent.namespace == "demo"
    assert intent.kind == "deployment"


def test_parser_resolves_statefulset_kind(parser):
    intent = parser.parse("session-store 起不来了")
    assert intent.workload == "session-store"
    assert intent.kind == "statefulset"


# ---------------------------------------------------------------------------
# 拒绝而不是猜（核心安全性质）
# ---------------------------------------------------------------------------


def test_unknown_workload_is_rejected(parser):
    """★ 用户提到不存在的工作负载时，必须明确拒绝，不能挑一个相近的顶替。"""
    with pytest.raises(IntentUnresolved) as e:
        parser.parse("订单服务 5xx 飙升了")
    msg = str(e.value)
    assert "无法" in msg or "不存在" in msg
    # 拒绝信息里要给出可选项，便于用户纠正
    assert "api-gateway" in msg


def test_vague_request_is_rejected(parser):
    with pytest.raises(IntentUnresolved):
        parser.parse("集群好像有问题")


def test_empty_text_is_rejected(parser):
    with pytest.raises(IntentUnresolved):
        parser.parse("   ")


def test_no_workloads_available_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("OMAGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = IntentParser(FakeK8s(deployments={"demo": []}, statefulsets={"demo": []}),
                     Policy(state_path=tmp_path / "s.json"))
    with pytest.raises(IntentUnresolved):
        p.parse("api-gateway 挂了")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_validate_rejects_hallucinated_workload(parser):
    """★ 即使 LLM 编出一个名字，也会在校验阶段被挡掉。"""
    avail = [{"namespace": "demo", "kind": "deployment", "name": "api-gateway"}]
    bogus = Intent(namespace="demo", workload="totally-made-up", source="llm")
    with pytest.raises(IntentUnresolved):
        parser._validate(bogus, avail)


def test_validate_auto_locates_unique_name_across_namespaces(parser):
    """同名工作负载只存在于一个命名空间时，应能自动定位。"""
    avail = [
        {"namespace": "demo", "kind": "deployment", "name": "api-gateway"},
        {"namespace": "staging", "kind": "deployment", "name": "other"},
    ]
    intent = parser._validate(Intent(workload="api-gateway", namespace=""), avail)
    assert intent.namespace == "demo"


def test_validate_fills_defaults(parser):
    avail = [{"namespace": "demo", "kind": "deployment", "name": "api-gateway"}]
    intent = parser._validate(Intent(workload="api-gateway"), avail)
    assert intent.interpretation          # 自动补一句复述
    assert intent.confidence in {"high", "medium", "low"}


def test_namespaces_come_from_policy(tmp_path):
    """只允许策略白名单里的命名空间被枚举。"""
    k8s = FakeK8s(deployments={"demo": ["a"], "production": ["secret-svc"]})
    pol = Policy(state_path=tmp_path / "s.json")
    wls = list_workloads(k8s, sorted(pol.allowed_namespaces))
    assert "secret-svc" not in {w["name"] for w in wls}


# ---------------------------------------------------------------------------
# 意图对象
# ---------------------------------------------------------------------------


def test_intent_to_dict_is_serialisable():
    import json

    d = Intent(workload="api-gateway", namespace="demo").to_dict()
    json.dumps(d)  # 不应抛异常
    assert d["planner"] == "rule"
    assert d["turns"] == 3


def test_parser_reports_unavailable_without_key(parser):
    assert parser.available is False
