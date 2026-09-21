"""安全内核的对抗测试。

这些用例的存在意义，是为了回答立项会上一定会被问到的那两个问题：

  "误操作怎么防？"   → 见 test_* 的拒绝类用例
  "做过对抗测试吗？" → 本文件

全部用例都不需要真实集群：安全的性质必须能在单元层面被证明，而不是靠演示时
"看起来没出事"。一个只在 happy path 上验证过的安全机制不算安全机制。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from omagent.agent import TOOLS, OpsAgent
from omagent.audit import AuditLog
from omagent.k8s import ActionRejected, PodInfo
from omagent.models import Decision, Evidence, Tier
from omagent.policy import Policy, PolicyError

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeK8s:
    """最小化的 K8s 替身，只实现安全内核真正用到的接口。"""

    def __init__(
        self,
        *,
        replicas: int = 3,
        stateful: bool = False,
        has_pvc: bool = False,
        labels: dict[str, str] | None = None,
        dry_run_ok: bool = True,
        healthy_pods: bool = True,
    ):
        self.replicas = replicas
        self.stateful = stateful
        self.has_pvc = has_pvc
        self.labels = labels or {"app": "api-gateway"}
        self.dry_run_ok = dry_run_ok
        self.healthy_pods = healthy_pods
        self.calls: list[tuple[str, dict, bool]] = []

    # --- 只读 ---
    def ping(self):
        return True, "fake-cluster"

    def read_workload(self, ns, kind, name):
        volumes = (
            [SimpleNamespace(persistent_volume_claim=SimpleNamespace(claim_name="data"))]
            if self.has_pvc
            else []
        )
        template = SimpleNamespace(
            metadata=SimpleNamespace(labels=self.labels),
            spec=SimpleNamespace(volumes=volumes),
        )
        spec = SimpleNamespace(
            replicas=self.replicas,
            selector=SimpleNamespace(match_labels=self.labels),
            template=template,
        )
        if self.stateful:
            spec.volume_claim_templates = [SimpleNamespace(metadata=SimpleNamespace(name="data"))]
        return SimpleNamespace(spec=spec, metadata=SimpleNamespace(labels=self.labels))

    def list_pods(self, namespace, label_selector=""):
        count = self.replicas if self.healthy_pods else 0
        return [
            PodInfo(
                name=f"pod-{i}",
                phase="Running",
                ready=True,
                restarts=0,
                namespace=namespace,
                labels=self.labels,
            )
            for i in range(count)
        ]

    def list_events(self, namespace, involved_name=""):
        return []

    def list_pdb(self, namespace):
        return [
            {
                "name": "api-gateway-pdb",
                "min_available": 2,
                "max_unavailable": None,
                "selector": self.labels,
                "disruptions_allowed": 1,
            }
        ]

    @property
    def core(self):
        return self

    def list_namespaced_service(self, namespace):
        return SimpleNamespace(items=[])

    # --- 写操作 ---
    def apply_mutation(self, tool, params, *, dry_run):
        if dry_run and not self.dry_run_ok:
            raise ActionRejected("dry-run 被 API Server 拒绝（模拟）")
        self.calls.append((tool, dict(params), dry_run))
        return f"[{'dry-run' if dry_run else 'EXECUTED'}] {tool}"


def isolated_policy(tmp_path) -> Policy:
    """每个测试用独立的状态文件。

    冷却状态现在是**落盘**的（修复"重启可绕过冷却期"）。如果不隔离，
    测试之间、以及测试与本地真实运行之间会互相污染——这个隔离问题正是
    在把状态持久化之后才暴露出来的。
    """
    return Policy(state_path=tmp_path / "policy-state.json")


def build(tmp_path, **kwargs):
    k8s = FakeK8s(**kwargs)
    agent = OpsAgent(
        k8s, isolated_policy(tmp_path), AuditLog(tmp_path / "audit.jsonl"),
        operator="tester@example.com",
    )
    return agent, k8s


GOOD_ENV = [Evidence("state", "Pod/pod-0", "OOMKilled，退出码 137", "test")]


def _params_for(spec):
    """按工具的目标类型给出最小合法参数。"""
    if spec.target == "node":
        return {"node": "om-sandbox-control-plane"}
    if spec.target == "pod":
        return {"namespace": "demo", "pod": "pod-0", "name": "pod-0"}
    if spec.target == "hpa":
        return {"namespace": "demo", "hpa": "api-gateway-hpa", "name": "api-gateway"}
    if spec.target == "namespace":
        return {"namespace": "demo", "name": "api-gateway"}
    return {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}


# ---------------------------------------------------------------------------
# 不变量 1：写操作永远需要人工批准
# ---------------------------------------------------------------------------


def test_mutating_tool_is_never_auto_approved(tmp_path):
    """任何 mutating 工具都必须要求批准——这是整个产品的根基。"""
    agent, _ = build(tmp_path)
    for name, spec in TOOLS.items():
        prop = agent.propose(name, _params_for(spec))
        if spec.mutating:
            assert prop.requires_approval, f"{name} 是写操作却不要求批准"
        else:
            assert not prop.requires_approval, f"{name} 是只读却要求批准"


def test_execute_without_decision_is_refused(tmp_path):
    """哪怕方案完全合法、dry-run 通过，没有批准也执行不了。"""
    agent, k8s = build(tmp_path)
    prop = agent.propose(
        "rollout_restart",
        {"namespace": "demo", "name": "api-gateway", "kind": "deployment"},
        rationale="恢复服务",
        evidence=GOOD_ENV,
    )
    assert prop.dry_run_ok and not prop.blocked, "前置条件：方案应当合法"

    res = agent.execute(prop, None)  # 没有 Decision

    assert res.status == "refused"
    assert "人工批准" in res.error
    # 关键：绝不能有任何真实写操作落到集群
    assert not [c for c in k8s.calls if not c[2]], "未批准的写操作被执行了！"


def test_execute_with_rejected_decision_does_not_write(tmp_path):
    agent, k8s = build(tmp_path)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    res = agent.execute(prop, Decision(prop.proposal_id, False, "tester", "太危险了"))
    assert res.status == "cancelled"
    assert not [c for c in k8s.calls if not c[2]]


def test_approval_for_a_different_proposal_is_rejected(tmp_path):
    """批准凭证与方案必须严格绑定，防止"批了 A 却执行了 B"。"""
    agent, k8s = build(tmp_path)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    forged = Decision("prop-someone-else", True, "tester", "批准")
    res = agent.execute(prop, forged)
    assert res.status == "refused"
    assert "不匹配" in res.error
    assert not [c for c in k8s.calls if not c[2]]


def test_approved_execution_succeeds(tmp_path):
    agent, k8s = build(tmp_path)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    res = agent.execute(prop, Decision(prop.proposal_id, True, "tester", "确认"))
    assert res.status == "success"
    assert [c for c in k8s.calls if not c[2]], "批准后应当真的执行"


# ---------------------------------------------------------------------------
# 不变量 2：T3 禁止动作，谁批都没用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["delete_workload", "delete_namespace", "delete_pvc", "modify_rbac", "read_secret"]
)
def test_t3_actions_are_blocked_even_with_approval(tmp_path, tool):
    agent, k8s = build(tmp_path)
    prop = agent.propose(tool, {"namespace": "demo", "name": "api-gateway"})
    assert prop.effective_tier is Tier.T3
    assert prop.blocked, f"{tool} 应当被熔断拦截"

    # 就算有人伪造一个 approved=True 的凭证，也不能执行
    res = agent.execute(prop, Decision(prop.proposal_id, True, "tester", "我就要删"))
    assert res.status == "refused"
    assert not [c for c in k8s.calls if not c[2]]


# ---------------------------------------------------------------------------
# 不变量 3：熔断规则
# ---------------------------------------------------------------------------


def test_namespace_outside_allowlist_is_blocked(tmp_path):
    agent, _ = build(tmp_path)
    prop = agent.propose(
        "rollout_restart", {"namespace": "production", "name": "api-gateway", "kind": "deployment"}
    )
    assert prop.blocked
    assert any("命名空间白名单" in b.rule for b in prop.breaches)


def test_protected_label_is_blocked(tmp_path):
    """带保护标签的负载，任何变更都要被拦住。"""
    agent, _ = build(tmp_path, labels={"app": "billing-core", "omagent.io/protected": "true"})
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "billing-core", "kind": "deployment"}
    )
    assert prop.blocked
    assert any("保护标签" in b.rule for b in prop.breaches)


def test_blast_radius_limit_is_enforced(tmp_path):
    """影响对象数超过上限必须硬拒绝。"""
    agent, _ = build(tmp_path, replicas=50)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    assert prop.blocked
    assert any("爆炸半径" in b.rule for b in prop.breaches)


def test_failed_dry_run_blocks_the_proposal(tmp_path):
    """服务端干跑没过，就不该生成一个可执行方案。"""
    agent, _ = build(tmp_path, dry_run_ok=False)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    assert prop.dry_run_ok is False
    assert prop.blocked
    assert any("dry-run" in b.rule for b in prop.breaches)


def test_singleton_stateful_escalates_confirm_strength(tmp_path):
    """单副本有状态服务：T1 必须升级为 T2 强确认。"""
    agent, _ = build(tmp_path, replicas=1, stateful=True, has_pvc=True)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "session-store", "kind": "statefulset"}
    )
    assert prop.tier is Tier.T1
    assert prop.effective_tier is Tier.T2, "单点有状态服务必须升级确认强度"
    assert prop.escalated_to is Tier.T2
    assert prop.effective_tier.confirm_strength == "strong"


def test_cooldown_blocks_repeated_changes(tmp_path):
    """5 分钟内不允许对同一工作负载重复变更，防止抖动式连续重启。"""
    agent, _ = build(tmp_path)
    params = {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}

    first = agent.propose("rollout_restart", params)
    assert not first.blocked
    agent.execute(first, Decision(first.proposal_id, True, "tester", "第一次"))

    second = agent.propose("rollout_restart", params)
    assert second.blocked
    assert any("变更冷却" in b.rule for b in second.breaches)


def test_freeze_window_forces_read_only(tmp_path):
    """冻结窗口内一切写操作降级为拒绝。"""
    import time as _time

    now = _time.localtime()
    window = f"{now.tm_hour:02d}:00-{(now.tm_hour + 1) % 24:02d}:00"
    k8s = FakeK8s()
    agent = OpsAgent(
        k8s, isolated_policy(tmp_path), AuditLog(tmp_path / "audit.jsonl"), operator="tester"
    )
    agent.policy.freeze_windows = [__import__("omagent.policy", fromlist=["_parse_window"])._parse_window(window)]

    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    assert prop.blocked
    assert any("冻结窗口" in b.rule for b in prop.breaches)


# ---------------------------------------------------------------------------
# 不变量 4：只读操作自动放行（不能把产品做成"什么都问"）
# ---------------------------------------------------------------------------


def test_readonly_tools_run_without_approval(tmp_path):
    agent, _ = build(tmp_path)
    prop = agent.propose("get_pods", {"namespace": "demo", "name": "api-gateway"})
    assert prop.tier is Tier.T0
    assert not prop.requires_approval
    assert not prop.blocked


# ---------------------------------------------------------------------------
# 不变量 5：配置与代码不一致时拒绝启动
# ---------------------------------------------------------------------------


def test_mutating_mismatch_between_code_and_policy_fails_closed(tmp_path):
    """如果有人把写操作在策略里标成只读，Agent 必须拒绝启动。"""
    import yaml

    src = yaml.safe_load(Policy().path.read_text(encoding="utf-8"))
    for a in src["actions"]:
        if a["name"] == "rollout_restart":
            a["mutating"] = False  # 偷偷把写操作标成只读
    bad = tmp_path / "bad-policy.yaml"
    bad.write_text(yaml.safe_dump(src, allow_unicode=True), encoding="utf-8")

    with pytest.raises(PolicyError, match="mutating"):
        OpsAgent(
            FakeK8s(),
            Policy(bad, state_path=tmp_path / "s.json"),
            AuditLog(tmp_path / "a.jsonl"),
        )


def test_unregistered_action_is_rejected(tmp_path):
    agent, _ = build(tmp_path)
    with pytest.raises(PolicyError, match="未在受管白名单"):
        agent.propose("rm_rf_everything", {"namespace": "demo", "name": "x"})


# ---------------------------------------------------------------------------
# 不变量 6：审计不可篡改 + 绕过门禁必留痕
# ---------------------------------------------------------------------------


def test_audit_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.log_intent("t1", "alice", "诊断 api-gateway")
    audit.log_refusal("t2", "alice", "删除 demo ns", "delete_namespace:删除命名空间", "不要这样做")
    ok, msg = audit.verify()
    assert ok, msg

    # 篡改一条记录的内容
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["operator"] = "mallory"
    lines[0] = json.dumps(rec, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, msg = audit.verify()
    assert not ok
    assert "篡改" in msg


def test_audit_chain_detects_deletion(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    for i in range(4):
        audit.log_intent(f"t{i}", "alice", f"动作 {i}")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    del lines[1]  # 删掉中间一条，试图掩盖
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, msg = audit.verify()
    assert not ok


def test_gate_violation_attempts_are_recorded(tmp_path):
    """未批准的写操作尝试必须留痕，这是事后追责的依据。"""
    agent, _ = build(tmp_path)
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    agent.execute(prop, None)  # 尝试绕过

    events = [r["event"] for r in agent.audit.records()]
    assert "gate_violation" in events
    violations = [r for r in agent.audit.records() if r["event"] == "gate_violation"]
    assert any(v["payload"]["reason"] == "missing_approval" for v in violations)


# ---------------------------------------------------------------------------
# 不变量 7：只读模式
# ---------------------------------------------------------------------------


def test_read_only_mode_refuses_all_writes(tmp_path):
    k8s = FakeK8s()
    agent = OpsAgent(
        k8s,
        isolated_policy(tmp_path),
        AuditLog(tmp_path / "audit.jsonl"),
        operator="tester",
        dry_run_only=True,
    )
    prop = agent.propose(
        "rollout_restart", {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}
    )
    res = agent.execute(prop, Decision(prop.proposal_id, True, "tester", "批准"))
    assert res.status == "refused"
    assert "只读模式" in res.error
    assert not [c for c in k8s.calls if not c[2]]


# ---------------------------------------------------------------------------
# 不变量 8：冷却状态必须持久化（重启不可绕过）
# ---------------------------------------------------------------------------


def test_cooldown_survives_process_restart(tmp_path):
    """★ 回归：冷却状态原先只在内存里，重启 Agent 即可绕过。

    做法：用一个状态文件走完"进程1 记录 → 进程2 读取"的路径。
    """
    state = tmp_path / "policy-state.json"
    key = "demo/Deployment/api-gateway"

    p1 = Policy(state_path=state)
    assert p1.cooldown_remaining(key) == 0.0
    p1.record_change(key)
    assert p1.cooldown_remaining(key) > 0

    # 模拟进程重启：全新的 Policy 对象，但状态文件还在
    p2 = Policy(state_path=state)
    assert p2.cooldown_remaining(key) > 0, "重启后冷却状态丢失——绕过漏洞仍在"


def test_expired_cooldown_is_not_restored(tmp_path):
    """已过冷却期的记录不该被恢复（避免状态文件无限增长且无意义）。"""
    import json as _json
    import time as _time

    state = tmp_path / "policy-state.json"
    old_ts = _time.time() - 10_000  # 远超过默认 300s 冷却期
    state.write_text(
        _json.dumps({"last_change": {"demo/Deployment/old": old_ts}}), encoding="utf-8"
    )
    p = Policy(state_path=state)
    assert p.cooldown_remaining("demo/Deployment/old") == 0.0


def test_corrupt_state_file_does_not_break_policy(tmp_path):
    """状态文件损坏不应让 Agent 起不来——安全机制要 fail-closed，可用性要 fail-safe。"""
    state = tmp_path / "policy-state.json"
    state.write_text("{ not json at all", encoding="utf-8")
    p = Policy(state_path=state)  # 不应抛异常
    assert p.cooldown_remaining("anything") == 0.0


def test_cooldown_actually_blocks_a_real_proposal(tmp_path):
    """端到端：持久化的冷却期确实能拦住第二次变更。"""
    agent, k8s = build(tmp_path)
    params = {"namespace": "demo", "name": "api-gateway", "kind": "deployment"}

    first = agent.propose("rollout_restart", params)
    assert not first.blocked
    agent.execute(first, Decision(first.proposal_id, True, "tester", "第一次"))

    # 用同一个状态文件新建 agent（模拟重启），冷却期应依然生效
    agent2 = OpsAgent(
        k8s,
        Policy(state_path=agent.policy.state_path),
        AuditLog(tmp_path / "audit2.jsonl"),
        operator="tester",
    )
    second = agent2.propose("rollout_restart", params)
    assert second.blocked
    assert any("变更冷却" in b.rule for b in second.breaches)


# ---------------------------------------------------------------------------
# 不变量 9：审计日志必须支持多进程并发写入
# ---------------------------------------------------------------------------


def test_concurrent_appends_keep_the_chain_valid(tmp_path):
    """★ 回归：Web 服务与 CLI 是两个进程、写同一个审计文件。

    早期实现只做了进程内线程锁，且以**内存里的计数器**为准——
    两个进程交叉写入会让 seq 断裂、哈希链失效，
    于是 ``verify()`` 会把正常日志报成"被篡改"。
    防篡改机制一旦误报，就等于失效。
    """
    import threading

    path = tmp_path / "audit.jsonl"

    def writer(n: int) -> None:
        log = AuditLog(path)  # 每个 writer 独立实例，模拟独立进程
        for i in range(25):
            log.append("evt", {"writer": n, "i": i})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log = AuditLog(path)
    ok, msg = log.verify()
    assert ok, f"并发写入破坏了哈希链：{msg}"
    assert len(log.records()) == 100
    # seq 必须严格连续
    seqs = [r["seq"] for r in log.records()]
    assert seqs == list(range(1, 101)), "seq 不连续"
