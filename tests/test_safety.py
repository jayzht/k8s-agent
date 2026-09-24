"""安全内核的对抗测试。

这里守的是一条产品主张：**看东西不用问，动东西要问。**
每条测试都在试图绕过它。
"""

from __future__ import annotations

import json

import pytest

from omagent.agent import GateViolation, OpsAgent
from omagent.audit import AuditLog
from omagent.models import Decision, Proposal, Target
from omagent.tools import FORBIDDEN, READONLY_TOOLS, TOOLS, WRITE_TOOLS, openai_tool_schema

from .fakes import FakeK8s


@pytest.fixture()
def agent(tmp_path):
    k8s = FakeK8s()
    audit = AuditLog(tmp_path / "audit.jsonl")
    return OpsAgent(k8s, audit, operator="tester"), k8s, audit


# ── 工具登记表本身的一致性 ────────────────────────────────────────────────


def test_每工具必属且仅属一类():
    assert set(READONLY_TOOLS) | set(WRITE_TOOLS) == set(TOOLS)
    assert not (set(READONLY_TOOLS) & set(WRITE_TOOLS))


def test_工具不能在登记表里同时是被禁止的():
    """禁止动作绝不出现在模型可见的清单里。"""
    assert not (set(TOOLS) & set(FORBIDDEN))


def test_每个工具都有参数schema且必需字段在properties里():
    for spec in TOOLS.values():
        assert spec.schema.get("type") == "object", spec.name
        props = set(spec.schema.get("properties", {}))
        missing = [r for r in spec.schema.get("required", []) if r not in props]
        assert not missing, f"{spec.name} 的 required 里有未声明的字段：{missing}"


def test_模型看不到任何禁止动作():
    names = {t["function"]["name"] for t in openai_tool_schema()}
    assert names == set(TOOLS)
    assert not (names & set(FORBIDDEN))


def test_每个写工具都强制要求写理由():
    """卡片上的"为什么"不能是可选的——否则操作员只能凭对模型的信任点确认。"""
    from omagent.tools import RATIONALE_FIELD

    for name in WRITE_TOOLS:
        spec = TOOLS[name]
        assert RATIONALE_FIELD in spec.params, f"{name} 缺少 rationale 字段"
        assert RATIONALE_FIELD in spec.required, f"{name} 的 rationale 不是必填"

    for name in READONLY_TOOLS:
        assert RATIONALE_FIELD not in TOOLS[name].params, f"只读工具 {name} 不该要理由"


def test_未登记工具默认当写操作():
    from omagent.tools import is_mutating

    assert is_mutating("rollout_restart") is True
    assert is_mutating("get_pods") is False
    # fail closed：不认识的工具按写操作处理
    assert is_mutating("some_tool_that_does_not_exist") is True


# ── 只读路径：自动执行，但绝不碰写 ────────────────────────────────────────


def test_只读工具自动执行无需批准(agent):
    ag, k8s, _ = agent
    out = ag.run_readonly("get_pods", {"namespace": "demo"})
    assert "get_pods" in out
    assert k8s.readonly_calls == [("get_pods", {"namespace": "demo"})]


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_写工具走只读路径一律被拒(agent, tool):
    """核心不变量：取证循环里漏不进任何写操作。"""
    ag, k8s, _ = agent
    with pytest.raises(GateViolation, match="会改变集群状态"):
        ag.run_readonly(tool, {"namespace": "demo", "name": "x", "pod": "x", "node": "x",
                               "replicas": 1, "container": "c", "hpa": "h", "data": {"a": "b"}})
    assert k8s.readonly_calls == []


@pytest.mark.parametrize("tool", list(FORBIDDEN))
def test_禁止动作即使走只读路径也被拒(agent, tool):
    ag, _, _ = agent
    with pytest.raises(GateViolation):
        ag.run_readonly(tool, {"namespace": "demo"})


def test_只读参数里的未知字段被丢弃(agent):
    ag, k8s, _ = agent
    ag.run_readonly("get_pods", {"namespace": "demo", "evil": "rm -rf /", "label_selector": "app=x"})
    assert k8s.readonly_calls == [("get_pods", {"namespace": "demo", "label_selector": "app=x"})]


# ── 写入路径：没有批准凭证就执行不了 ──────────────────────────────────────


def test_没有批准凭证时写操作被拒(agent):
    ag, k8s, _ = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"}, rationale="测试理由")
    res = ag.execute_write(prop, None)
    assert res.status == "refused"
    assert "需要人工批准" in res.error
    # 关键：集群上什么都没发生（只有 dry-run 那一次）
    assert all(call[2] for call in k8s.mutation_calls)


def test_批准凭证与方案不匹配时被拒(agent):
    """批准 A 却想执行 B——经典调包攻击。"""
    ag, k8s, _ = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"}, rationale="测试理由")
    forged = Decision(proposal_id="prop-someone-else", approved=True, operator="attacker")
    res = ag.execute_write(prop, forged)
    assert res.status == "refused"
    assert "不匹配" in res.error
    assert all(call[2] for call in k8s.mutation_calls)


def test_匹配的批准凭证才能真的执行(agent):
    ag, k8s, _ = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"}, rationale="测试理由")
    ok = Decision(proposal_id=prop.proposal_id, approved=True, operator="tester")
    res = ag.execute_write(prop, ok)
    assert res.status == "success"
    assert any(not call[2] for call in k8s.mutation_calls)


def test_拒绝时不执行(agent):
    ag, k8s, _ = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"}, rationale="测试理由")
    no = Decision(proposal_id=prop.proposal_id, approved=False, operator="tester", reason="再看看")
    res = ag.execute_write(prop, no)
    assert res.status == "cancelled"
    assert all(call[2] for call in k8s.mutation_calls)


@pytest.mark.parametrize("tool", list(FORBIDDEN))
def test_禁止动作任何人都批不了(agent, tool):
    ag, k8s, _ = agent
    with pytest.raises(GateViolation):
        ag.propose(tool, {"namespace": "demo", "name": "x"}, rationale="测试理由")
    # 就算绕过 propose 手搓一个 Proposal，execute_write 也拦得住
    forged = Proposal(tool=tool, params={"namespace": "demo"}, target=Target("X", "demo", "x"))
    res = ag.execute_write(forged, Decision(forged.proposal_id, True, "root"))
    assert res.status == "refused"
    assert all(call[2] for call in k8s.mutation_calls)


def test_写操作不能指向允许列表外的命名空间(agent):
    ag, k8s, _ = agent
    with pytest.raises(GateViolation, match="不在写操作允许列表"):
        ag.propose("rollout_restart", {"namespace": "kube-system", "name": "coredns"}, rationale="测试理由")
    assert all(call[2] for call in k8s.mutation_calls)


def test_只读不受命名空间限制(agent):
    """排查问题时必须能看 kube-system。"""
    ag, _, _ = agent
    assert ag.run_readonly("get_pods", {"namespace": "kube-system"})


def test_dry_run没过时人工批准也不执行(agent):
    ag, k8s, _ = agent
    k8s.dry_run_ok = False
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"}, rationale="测试理由")
    assert prop.dry_run_ok is False
    res = ag.execute_write(prop, Decision(prop.proposal_id, True, "tester"))
    assert res.status == "refused"
    assert "dry-run" in res.error
    assert all(call[2] for call in k8s.mutation_calls)


def test_只读工具不会被生成写方案(agent):
    ag, _, _ = agent
    with pytest.raises(GateViolation, match="只读工具"):
        ag.propose("get_pods", {"namespace": "demo"})


# ── 参数校验 ──────────────────────────────────────────────────────────────


def test_缺少必需参数时报错并说明缺什么(agent):
    ag, _, _ = agent
    with pytest.raises(GateViolation, match="缺少必需参数"):
        ag.run_readonly("get_pods", {})


def test_整数参数会被强制转换(agent):
    ag, k8s, _ = agent
    prop = ag.propose("scale_workload", {"namespace": "demo", "name": "api", "replicas": "4"}, rationale="测试理由")
    assert prop.params["replicas"] == 4


def test_整数参数无法转换时报错(agent):
    ag, _, _ = agent
    with pytest.raises(GateViolation, match="类型不对"):
        ag.propose("scale_workload", {"namespace": "demo", "name": "api", "replicas": "很多"}, rationale="测试理由")


def test_写方案里的未知字段被丢弃(agent):
    """模型不能靠多塞字段把东西夹带进 API 调用。"""
    ag, _, _ = agent
    prop = ag.propose("rollout_restart",
                      {"namespace": "demo", "name": "api", "force": True, "gracePeriod": 0},
                      rationale="测试理由")
    assert prop.params == {"namespace": "demo", "name": "api"}


def test_写方案必须说明理由(agent):
    """没有理由的写方案根本构造不出来——卡片上不能只剩"信不信模型"。"""
    ag, _, _ = agent
    with pytest.raises(GateViolation, match="rationale"):
        ag.propose("rollout_restart", {"namespace": "demo", "name": "api"})


def test_理由写在参数里也能被提取出来(agent):
    ag, _, _ = agent
    prop = ag.propose("rollout_restart",
                      {"namespace": "demo", "name": "api", "rationale": "内存上限被改小导致 OOM"})
    assert prop.rationale == "内存上限被改小导致 OOM"
    # 理由不进 params，不会被发给 API Server
    assert "rationale" not in prop.params


# ── 审计 ──────────────────────────────────────────────────────────────────


def test_门禁拒绝会留下审计记录(agent):
    ag, _, audit = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"}, rationale="测试理由")
    ag.execute_write(prop, Decision(proposal_id="wrong", approved=True, operator="attacker"))
    events = [r["event"] for r in audit.records()]
    assert "gate_violation" in events
    ok, msg = audit.verify()
    assert ok, msg


def test_审计链能检出篡改(agent, tmp_path):
    ag, _, audit = agent
    ag.run_readonly("get_pods", {"namespace": "demo"})
    ag.run_readonly("get_events", {"namespace": "demo"})
    assert audit.verify()[0]

    # 直接把文件里的一行改掉
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"demo"', '"prod"')
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, msg = AuditLog(tmp_path / "audit.jsonl").verify()
    assert not ok
    assert "篡改" in msg


# ── 影响面：必须按"改完之后"算 ────────────────────────────────────────────
#
# 早期实现的 impact.replicas 读的是当前副本数，跟要改成多少无关。
# 结果：把 3 缩到 1 和把 3 扩到 10 给出**同一张卡片**，
# 而前者会把一个有冗余的服务变成单点——那正是操作员最需要看到的信息。


@pytest.fixture()
def impact_agent(tmp_path):
    """让 read_workload 返回一个指定副本数的工作负载。

    复用 fakes.workload() 而不是临时搭一个：假对象必须把真实对象的结构补全，
    否则 impact.py 读不到字段会抛异常、被 _analyse 吞掉，
    影响面变成一片空白——测试会"通过"但什么都没验证到。
    """
    from .fakes import workload as _wl

    class K:
        def __init__(self, replicas):
            self.replicas = replicas
            self.readonly_calls = []

        def read_workload(self, ns, kind, name):
            return _wl(name, kind, replicas=self.replicas)

        def list_pods(self, ns, label_selector=""):
            return []

        def services_selecting(self, ns, labels):
            return []

        def apply_mutation(self, tool, params, *, dry_run):
            return "ok"

        def read_only_query(self, tool, params):
            return "ok"

    def make(replicas):
        k8s = K(replicas)
        audit = AuditLog(tmp_path / "audit.jsonl")
        ag = OpsAgent(k8s, audit, operator="t")
        return ag, k8s

    return make


def test_缩容的影响面按目标副本数算(impact_agent):
    ag, _ = impact_agent(3)
    prop = ag.propose("scale_workload",
                      {"namespace": "demo", "name": "api", "replicas": 1},
                      rationale="测试")
    i = prop.impact
    assert i.replicas == 3
    assert i.target_replicas == 1
    assert i.pods_removed == 2
    assert i.single_point is True, "缩到 1 之后就是单点了，必须标出来"
    assert any("缩减到 1" in n for n in i.notes)


def test_扩容的影响面和缩容不一样(impact_agent):
    ag, _ = impact_agent(3)
    prop = ag.propose("scale_workload",
                      {"namespace": "demo", "name": "api", "replicas": 6},
                      rationale="测试")
    i = prop.impact
    assert i.target_replicas == 6
    assert i.pods_added == 3
    assert i.pods_removed == 0
    assert i.single_point is False


def test_缩到0会明确警告(impact_agent):
    ag, _ = impact_agent(2)
    prop = ag.propose("scale_workload",
                      {"namespace": "demo", "name": "api", "replicas": 0},
                      rationale="测试")
    assert any("完全没有实例" in n for n in prop.impact.notes)


def test_非_scale_操作不受目标副本数影响(impact_agent):
    ag, _ = impact_agent(3)
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试")
    assert prop.impact.target_replicas is None
    assert prop.impact.single_point is False


def test_单点服务的重启仍会被标成单点(impact_agent):
    ag, _ = impact_agent(1)
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试")
    assert prop.impact.single_point is True


# ── 执行前重新干跑（TOCTOU）──────────────────────────────────────────────


def test_批准后重新干跑失败则拒绝执行(agent):
    """卡片上的 dry-run 是**生成时**的结果，人可能几分钟后才点批准。
    这中间集群会变。拿陈旧结论执行，正是运维里最危险的时间窗。"""
    ag, k8s, _ = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试")
    assert prop.dry_run_ok is True

    k8s.dry_run_ok = False  # 审批期间集群变了 / 对象被删了

    res = ag.execute_write(prop, Decision(prop.proposal_id, True, "tester"))
    assert res.status == "refused"
    assert "重新干跑" in res.error
    assert all(c[2] for c in k8s.mutation_calls), "拒绝了就不该有真实执行"


def test_批准后干跑结果变化会留痕(agent):
    ag, k8s, audit = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试")
    k8s.dry_run_result = "集群状态变了"
    ag.execute_write(prop, Decision(prop.proposal_id, True, "tester"))
    assert "dry_run_changed" in [r["event"] for r in audit.records()]


def test_干跑一直通过就正常执行(agent):
    ag, k8s, _ = agent
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试")
    res = ag.execute_write(prop, Decision(prop.proposal_id, True, "tester"))
    assert res.status == "success"
    assert any(not c[2] for c in k8s.mutation_calls)


# ── 语义记忆：案例库 ──────────────────────────────────────────────────────


def test_只记录人工批准且成功的处置(agent, tmp_path):
    """案例库的可信度全部来自这条约束：库里每一条都有责任主体，
    而不是模型自己写进去的猜测。"""
    from omagent.cases import CaseStore

    ag, _, _ = agent
    store = CaseStore(tmp_path / "cases.jsonl")
    ag.cases = store

    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试理由")
    prop.signature = ["oom_killed", "not_ready"]

    from omagent.models import Decision, ExecutionResult

    ok = ExecutionResult(proposal_id=prop.proposal_id, status="success", output="好了")
    bad = ExecutionResult(proposal_id=prop.proposal_id, status="failed", error="炸了")
    yes = Decision(proposal_id=prop.proposal_id, approved=True, operator="zhang.wei")
    no = Decision(proposal_id=prop.proposal_id, approved=False, operator="zhang.wei")

    # 四个缺一不可的组合，都不该记
    assert ag.record_case(prop, None, ok) is False          # 没有批准凭证
    assert ag.record_case(prop, no, ok) is False            # 被拒绝了
    assert ag.record_case(prop, yes, bad) is False          # 批准了但执行失败
    assert ag.record_case(prop, yes, None) is False         # 没有结果
    assert store.all() == [], "不该有任何一条被记进来"

    assert ag.record_case(prop, yes, ok, operator="zhang.wei") is True
    cases = store.all()
    assert len(cases) == 1
    assert cases[0].operator == "zhang.wei"
    assert cases[0].signature == ["oom_killed", "not_ready"]


def test_没有症状签名就不记(tmp_path):
    """签名是空的说明当时没测出异常——记下来只会污染案例库。"""
    from omagent.cases import CaseStore

    k8s = FakeK8s()
    ag = OpsAgent(k8s, AuditLog(tmp_path / "a.jsonl"), operator="t",
                  cases=CaseStore(tmp_path / "c.jsonl"))
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="x")
    prop.signature = []
    from omagent.models import Decision, ExecutionResult

    ag.record_case(prop,
                   Decision(proposal_id=prop.proposal_id, approved=True, operator="t"),
                   ExecutionResult(proposal_id=prop.proposal_id, status="success"))
    assert ag.cases.all() == []


# ── 案例回查：执行成功 ≠ 问题解决 ────────────────────────────────────────
#
# apply_mutation 返回 success 只说明 API Server 收了请求。内存上限改完
# Pod 照样 OOM，那次调用**也是 success**。直接当经验教出去，下次检索到的
# 就是一条人工批准过、却根本没用的先例。

from omagent.cases import (  # noqa: E402
    OUTCOME_APPLIED,
    OUTCOME_EFFECTIVE,
    OUTCOME_INEFFECTIVE,
    Case,
    CaseStore,
)


def _case(store, case_id, outcome=OUTCOME_APPLIED, sig=None):
    c = Case(signature=sig or ["oom_killed", "unhealthy"],
             namespace="demo", workload="api", kind="Deployment",
             tool="patch_resources", rationale="r", operator="zhang.wei",
             outcome=outcome, ts="2026-01-01T00:00:00", case_id=case_id)
    store.append(c)
    return c


def test_未回查的案例不参与检索(tmp_path):
    """这是核心改动：applied 状态的知识**不许**被当成经验。"""
    store = CaseStore(tmp_path / "c.jsonl")
    _case(store, "c1", OUTCOME_APPLIED)
    assert store.search(["oom_killed", "unhealthy"]) == []
    assert store.count_for(["oom_killed", "unhealthy"]) == 0


def test_回查有效的案例才能被检索(tmp_path):
    store = CaseStore(tmp_path / "c.jsonl")
    _case(store, "c1", OUTCOME_EFFECTIVE)
    assert len(store.search(["oom_killed", "unhealthy"])) == 1


def test_无效案例不参与检索(tmp_path):
    store = CaseStore(tmp_path / "c.jsonl")
    _case(store, "c1", OUTCOME_INEFFECTIVE)
    assert store.search(["oom_killed", "unhealthy"]) == []


def test_同_case_id_以最后一条为准(tmp_path):
    store = CaseStore(tmp_path / "c.jsonl")
    _case(store, "c1", OUTCOME_APPLIED)
    store.update("c1", outcome=OUTCOME_EFFECTIVE, evidence="症状消失了")
    cases = store.all()
    assert len(cases) == 1, "同一个 case 的多条记录应折叠成一条"
    assert cases[0].outcome == OUTCOME_EFFECTIVE
    assert cases[0].evidence == "症状消失了"
    assert len(store.search(["oom_killed", "unhealthy"])) == 1


def test_无效回查会作废同症状同处置的旧案例(tmp_path):
    """这招现在不管用 → 更早那次"成功"也不可信，必须一并推翻。

    否则模型下次还会引用一条已经被证伪的经验。
    """
    store = CaseStore(tmp_path / "c.jsonl")
    _case(store, "old", OUTCOME_EFFECTIVE)
    assert len(store.search(["oom_killed", "unhealthy"])) == 1

    n = store.supersede(signature=["oom_killed", "unhealthy"],
                        tool="patch_resources", reason="测试")
    assert n == 1
    assert store.by_id("old").outcome == OUTCOME_INEFFECTIVE
    assert store.search(["oom_killed", "unhealthy"]) == []


def test_作废只影响同处置(tmp_path):
    store = CaseStore(tmp_path / "c.jsonl")
    _case(store, "a", OUTCOME_EFFECTIVE)
    other = Case(signature=["oom_killed", "unhealthy"], namespace="demo",
                 workload="api", kind="Deployment", tool="scale_workload",
                 operator="x", outcome=OUTCOME_EFFECTIVE, ts="2026-01-01T00:00:00",
                 case_id="b")
    store.append(other)
    store.supersede(signature=["oom_killed", "unhealthy"], tool="patch_resources")
    assert store.by_id("b").outcome == OUTCOME_EFFECTIVE, "不同处置不该被牵连"


def test_老格式的案例文件仍能读(tmp_path):
    """没有 case_id 的历史记录要能读出来，不能因为格式升级就全废掉。"""
    p = tmp_path / "c.jsonl"
    p.write_text(json.dumps({
        "signature": ["oom_killed"], "namespace": "demo", "workload": "api",
        "kind": "Deployment", "tool": "patch_resources", "operator": "old",
        "outcome": OUTCOME_EFFECTIVE, "ts": "2026-01-01T00:00:00",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    store = CaseStore(p)
    cases = store.all()
    assert len(cases) == 1 and cases[0].case_id
    assert len(store.search(["oom_killed"])) == 1


def test_写库时记的是_applied_而不是_success(agent, tmp_path):
    """这条保证"执行成功"不会被直接当成经验。"""
    from omagent.cases import CaseStore as CS
    from omagent.models import Decision, ExecutionResult

    ag, _, _ = agent
    ag.cases = CS(tmp_path / "c.jsonl")
    prop = ag.propose("rollout_restart", {"namespace": "demo", "name": "api"},
                      rationale="测试")
    prop.signature = ["oom_killed"]
    ag.record_case(prop,
                   Decision(proposal_id=prop.proposal_id, approved=True, operator="t"),
                   ExecutionResult(proposal_id=prop.proposal_id, status="success"))
    assert ag.cases.all()[0].outcome == OUTCOME_APPLIED
    assert ag.cases.search(["oom_killed"]) == [], "还没回查就不该被检索"
