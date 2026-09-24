"""会话循环的行为测试。

守两条：**只读自动跑**、**写入必须停下来等人**。
"""

from __future__ import annotations

import json

import pytest

from omagent.agent import GateViolation, OpsAgent
from omagent.audit import AuditLog
from omagent.session import PROPOSAL_TTL, ChatSession, _is_asking_permission

from .fakes import FakeK8s, ScriptedLLM, reply_text, reply_tools


@pytest.fixture()
def env(tmp_path):
    k8s = FakeK8s()
    audit = AuditLog(tmp_path / "audit.jsonl")
    agent = OpsAgent(k8s, audit, operator="tester")
    return agent, k8s, audit


def build(env, replies) -> ChatSession:
    agent, _, _ = env
    return ChatSession("test-session", agent, ScriptedLLM(replies), namespace="demo")


def run(sess: ChatSession, timeout: float = 5.0) -> None:
    """等后台循环跑完当前这一步。"""
    if sess._thread is not None:
        sess._thread.join(timeout)
    assert not sess._thread.is_alive(), "会话循环没有在超时内结束"


def types(sess: ChatSession) -> list[str]:
    return [e.type for e in sess.events]


# ── 只读：自动执行 ────────────────────────────────────────────────────────


def test_只读工具调用自动执行不需要批准(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("get_pods", {"namespace": "demo"})),
        reply_text("查完了，一切正常。"),
    ])
    sess.send("demo 有问题吗？")
    run(sess)

    assert k8s.readonly_calls == [("get_pods", {"namespace": "demo"})]
    assert sess.pending is None
    assert sess.status == "idle"
    assert "tool" in types(sess)
    assert "approval" not in types(sess)


def test_只读可以连续多轮自己跑(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("get_pods", {"namespace": "demo"})),
        reply_tools(("get_events", {"namespace": "demo"})),
        reply_tools(("get_logs", {"namespace": "demo", "pod": "api-1"})),
        reply_text("结论：内存不够。"),
    ])
    sess.send("api 一直在重启")
    run(sess)

    assert [c[0] for c in k8s.readonly_calls] == ["get_pods", "get_events", "get_logs"]
    assert sess.status == "idle"


def test_同一条回复里的多个只读调用都会被执行(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("get_pods", {"namespace": "demo"}), ("get_events", {"namespace": "demo"})),
        reply_text("看完了。"),
    ])
    sess.send("看看")
    run(sess)
    assert [c[0] for c in k8s.readonly_calls] == ["get_pods", "get_events"]


def test_工具结果会回灌给模型(env):
    agent, _, _ = env
    llm = ScriptedLLM([
        reply_tools(("get_pods", {"namespace": "demo"})),
        reply_text("好了"),
    ])
    sess = ChatSession("s", agent, llm, namespace="demo")
    sess.send("看看")
    run(sess)

    second_call_messages = llm.calls[1]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert tool_msgs and "fake-readonly-output" in tool_msgs[0]["content"]


# ── 写入：停下来等人 ──────────────────────────────────────────────────────


def test_写操作会挂起等人批准且不执行(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api-gateway", "rationale": "OOM 重启，重启以加载新配置"})),
    ])
    sess.send("重启一下 api-gateway")
    run(sess)

    assert sess.status == "awaiting_approval"
    assert sess.pending is not None
    assert sess.pending.proposal.tool == "rollout_restart"
    # 只发生了 dry-run，没有真的改
    assert k8s.mutation_calls and all(c[2] for c in k8s.mutation_calls)
    assert "approval" in types(sess)


def test_批准后才真的执行(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api-gateway", "rationale": "OOM 重启，重启以加载新配置"})),
        reply_text("已经重启完成。"),
    ])
    sess.send("重启一下")
    run(sess)

    prop_id = sess.pending.proposal.proposal_id
    sess.resolve_approval(prop_id, True, "确认")
    run(sess)

    assert any(not c[2] for c in k8s.mutation_calls), "批准后应该有一次真实执行"
    assert sess.status == "idle"
    assert "execution" in types(sess)
    assert sess.pending is None


def test_拒绝后不执行且把拒绝原因告诉模型(env):
    agent, k8s, _ = env
    llm = ScriptedLLM([
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api-gateway", "rationale": "OOM 重启，重启以加载新配置"})),
        reply_text("好的，那我不重启了。"),
    ])
    sess = ChatSession("s", agent, llm, namespace="demo")
    sess.send("重启一下")
    run(sess)

    prop_id = sess.pending.proposal.proposal_id
    sess.resolve_approval(prop_id, False, "现在别重启")
    run(sess)

    assert all(c[2] for c in k8s.mutation_calls), "拒绝后不该有真实执行"
    assert sess.status == "idle"
    assert "decision" in types(sess)

    resumed = llm.calls[-1]
    tool_msgs = [m for m in resumed if m.get("role") == "tool"]
    assert any("拒绝了" in m["content"] for m in tool_msgs)


def test_批准编号不对会被拒绝(env):
    agent, _, _ = env
    sess = build(env, [reply_tools(("rollout_restart", {"namespace": "demo", "name": "api", "rationale": "重启以恢复"}))])
    sess.send("重启")
    run(sess)

    with pytest.raises(GateViolation, match="不匹配"):
        sess.resolve_approval("prop-forged", True)


def test_没有待批方案时批准会报错(env):
    agent, _, _ = env
    sess = build(env, [reply_text("没事")])
    sess.send("看看")
    run(sess)
    with pytest.raises(GateViolation, match="没有待确认的方案"):
        sess.resolve_approval("prop-x", True)


def test_同一条回复里写操作后面的只读调用在批准后继续(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(
            ("get_pods", {"namespace": "demo"}),
            ("rollout_restart", {"namespace": "demo", "name": "api", "rationale": "重启以恢复"}),
            ("get_events", {"namespace": "demo"}),
        ),
        reply_text("完事。"),
    ])
    sess.send("处理一下")
    run(sess)

    # 写操作之前的那次只读已经跑了，之后的那次还在队列里等着
    assert [c[0] for c in k8s.readonly_calls] == ["get_pods"]
    assert sess.status == "awaiting_approval"

    sess.resolve_approval(sess.pending.proposal.proposal_id, True)
    run(sess)

    assert [c[0] for c in k8s.readonly_calls] == ["get_pods", "get_events"]
    assert sess.status == "idle"


# ── 异常与边界 ────────────────────────────────────────────────────────────


def test_禁止动作会被拦下并告诉模型(env):
    agent, k8s, _ = env
    llm = ScriptedLLM([
        reply_tools(("delete_namespace", {"namespace": "demo"})),
        reply_text("这个我不能做。"),
    ])
    sess = ChatSession("s", agent, llm, namespace="demo")
    sess.send("把 demo 删了")
    run(sess)

    assert sess.status == "idle"
    assert not k8s.mutation_calls
    assert not k8s.readonly_calls
    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    assert any("禁止" in m["content"] for m in tool_msgs)


def test_参数错误会回灌给模型而不是崩掉(env):
    agent, _, _ = env
    llm = ScriptedLLM([
        reply_tools(("get_pods", {})),  # 少了 namespace
        reply_text("我重试。"),
    ])
    sess = ChatSession("s", agent, llm, namespace="demo")
    sess.send("看看")
    run(sess)

    assert sess.status == "idle"
    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    assert any("缺少必需参数" in m["content"] for m in tool_msgs)


def test_参数不是合法json时回灌错误(env):
    from omagent.llm import LLMReply, ToolCall

    agent, _, _ = env
    bad = ToolCall(id="c1", name="get_pods", arguments={}, parse_error="参数不是合法 JSON")
    llm = ScriptedLLM([LLMReply(tool_calls=[bad]), reply_text("重试")])
    sess = ChatSession("s", agent, llm, namespace="demo")
    sess.send("看看")
    run(sess)

    assert sess.status == "idle"
    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    assert any("JSON" in m["content"] for m in tool_msgs)


def test_模型挂了会给出一句人话而不是堆栈(env):
    from omagent.llm import LLMError

    agent, _, _ = env

    class Boom:
        available = True

        def chat(self, *a, **k):
            raise LLMError("模型接口 500")

    sess = ChatSession("s", agent, Boom(), namespace="demo")
    sess.send("看看")
    run(sess)

    assert sess.status == "error"
    errs = [e for e in sess.events if e.type == "error"]
    assert errs and "模型调用失败" in errs[0].data["message"]


def test_正在思考时不允许再发消息(env):
    agent, _, _ = env
    sess = build(env, [reply_text("嗯")])
    sess.status = "thinking"
    with pytest.raises(GateViolation, match="还在处理"):
        sess.send("喂")


def test_有待批方案时不允许插话(env):
    agent, _, _ = env
    sess = build(env, [reply_text("嗯")])
    sess.status = "awaiting_approval"
    with pytest.raises(GateViolation, match="等着你确认"):
        sess.send("喂")


def test_空消息被拒(env):
    agent, _, _ = env
    sess = build(env, [reply_text("嗯")])
    with pytest.raises(GateViolation, match="不能为空"):
        sess.send("   ")


# ── 轮次预算 ──────────────────────────────────────────────────────────────


def test_工具调用不消耗轮次预算(env):
    """一轮里查 4 个工具是 1 轮，不是 4 轮。

    早期实现把工具调用也计进 MAX_STEPS，导致一次正常的排查
    （读 pods → 读事件 → 读日志 → 再看规格）还没查完就报"达到最大轮次"。
    """
    from omagent.session import MAX_STEPS

    agent, k8s, _ = env
    rounds = MAX_STEPS - 3  # 每轮 4 个工具调用，总轮数仍在上限内
    replies = [
        reply_tools(
            ("get_pods", {"namespace": "demo"}),
            ("get_events", {"namespace": "demo"}),
            ("get_services", {"namespace": "demo"}),
            ("get_endpoints", {"namespace": "demo"}),
        )
        for _ in range(rounds)
    ]
    replies.append(reply_text("查完了。"))
    sess = build(env, replies)
    sess.send("查一下")
    run(sess)

    assert sess.status == "idle", "不该因为工具调用多就判定超轮次"
    assert len(k8s.readonly_calls) == rounds * 4


def test_真的超过轮次上限时才报错(env):
    from omagent.session import MAX_STEPS

    agent, _, _ = env
    replies = [reply_tools(("get_pods", {"namespace": "demo"})) for _ in range(MAX_STEPS + 3)]
    sess = build(env, replies)
    sess.send("一直查")
    run(sess)

    assert sess.status == "error"
    errs = [e for e in sess.events if e.type == "error"]
    assert errs and "轮" in errs[0].data["message"]


# ── 事件游标 ──────────────────────────────────────────────────────────────
#
# POST 的返回值必须带着**这次调用期间产生的事件**。早期版本返回
# snapshot(self._seq)（调用后的最新序号），于是调用期间的事件全部落在游标之外，
# 前端永远收不到——表现就是"点了批准，执行结果不显示"。


def test_发消息的返回值包含本次事件(env):
    sess = build(env, [reply_text("嗯")])
    snap = sess.send("你好")
    kinds = [e["type"] for e in snap["only_events"]]
    assert "user" in kinds
    assert snap["last_seq"] >= max(e["seq"] for e in snap["only_events"])


def test_批准的返回值包含裁决与执行事件(env):
    agent, _, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好了"),
    ])
    sess.send("重启")
    run(sess)

    snap = sess.resolve_approval(sess.pending.proposal.proposal_id, True, "确认")
    kinds = [e["type"] for e in snap["only_events"]]
    assert "decision" in kinds, "裁决事件必须回传给前端"
    assert "execution" in kinds, "执行结果必须回传给前端"


def test_拒绝的返回值也包含裁决事件(env):
    agent, _, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好的"),
    ])
    sess.send("重启")
    run(sess)

    snap = sess.resolve_approval(sess.pending.proposal.proposal_id, False, "先别动")
    kinds = [e["type"] for e in snap["only_events"]]
    assert "decision" in kinds
    assert "execution" not in kinds


# ── 历史裁剪 ──────────────────────────────────────────────────────────────


def test_裁剪历史不会切断tool_calls配对():
    from omagent.session import PROPOSAL_TTL, ChatSession as CS

    messages = [{"role": "system", "content": "s"}]
    for i in range(40):
        messages.append({"role": "user", "content": "x" * 5000})
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "get_pods", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 5000})

    trimmed = CS._trim(messages)
    assert sum(len(str(m.get("content") or "")) for m in trimmed) <= 120_000
    # 每一条 tool 消息前面必须有一条带 tool_calls 的 assistant
    for idx, m in enumerate(trimmed):
        if m.get("role") == "tool":
            assert idx > 0 and trimmed[idx - 1].get("tool_calls") or trimmed[idx - 1].get("role") == "tool"


# ── 待批方案的有效期 ──────────────────────────────────────────────────────
#
# 卡片上的影响面、dry-run、目标副本数都是**生成那一刻**的快照。
# 一张挂了两小时的卡片被批准时，集群早就不是那个样子了。
# 让人对着过期信息做决定，比不给他信息更糟——他会以为自己看过了。


def test_过期的方案批准了也不执行(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好的"),
    ])
    sess.send("重启")
    run(sess)
    assert sess.status == "awaiting_approval"

    # 把方案"变老"
    sess.pending.created_at -= PROPOSAL_TTL + 10

    snap = sess.resolve_approval(sess.pending.proposal.proposal_id, True, "确认")
    kinds = [e["type"] for e in snap["only_events"]]
    assert "approval_expired" in kinds
    assert "execution" not in kinds, "过期方案绝不能被执行"
    assert all(c[2] for c in k8s.mutation_calls), "过期后不该有任何真实执行"


def test_过期后告诉模型重新读现状(env):
    agent, _, _ = env
    llm = ScriptedLLM([
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好，我重新看一遍"),
    ])
    sess = ChatSession("s", agent, llm, namespace="demo")
    sess.send("重启")
    run(sess)
    sess.pending.created_at -= PROPOSAL_TTL + 10
    sess.resolve_approval(sess.pending.proposal.proposal_id, True)
    run(sess)

    tool_msgs = [m for m in llm.calls[-1] if m.get("role") == "tool"]
    assert any("过期作废" in m["content"] for m in tool_msgs)


def test_未过期的方案正常执行(env):
    agent, k8s, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好了"),
    ])
    sess.send("重启")
    run(sess)
    snap = sess.resolve_approval(sess.pending.proposal.proposal_id, True, "确认")
    assert "execution" in [e["type"] for e in snap["only_events"]]


def test_快照里带有效期倒计时(env):
    agent, _, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
    ])
    sess.send("重启")
    run(sess)
    p = sess.snapshot(0)["pending"]
    assert p["ttl_seconds"] == PROPOSAL_TTL
    assert 0 < p["expires_in"] <= PROPOSAL_TTL


# ── 历史卡片必须能被识别为"已裁决" ────────────────────────────────────────
#
# 重开页面会从磁盘重放整个事件流，里面包含早就裁决过的审批卡片。
# 前端要能判断一张卡片是"待办"还是"记录"，依据就是：**有没有一个
# proposal_id 相同的 decision 事件**。这条契约断了，历史卡片就会
# 重新变成可点的按钮，而倒计时还会从 15:00 重新开始（expires_in 是
# 当初发事件时的快照，重放时早就不代表现实了）。


def test_审批事件带_proposal_id_能和裁决对上(env):
    agent, _, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好了"),
    ])
    sess.send("重启")
    run(sess)
    pid = sess.pending.proposal.proposal_id
    sess.resolve_approval(pid, True, "确认")
    run(sess)

    evs = sess.snapshot(0)["only_events"]
    apps = [e for e in evs if e["type"] == "approval"]
    decs = [e for e in evs if e["type"] == "decision"]
    execs = [e for e in evs if e["type"] == "execution"]

    assert apps and decs and execs
    assert apps[0]["data"]["proposal_id"] == pid
    assert decs[0]["data"]["proposal_id"] == pid, "裁决事件必须带上 proposal_id，否则前端认不出是哪张卡片"
    assert execs[0]["data"]["proposal_id"] == pid
    assert decs[0]["data"]["approved"] is True


def test_重放历史时待批状态是空的(env):
    """已经裁决过的会话重新加载时，pending 必须是 None——
    否则前端会把一张老卡片当成"当前待办"。"""
    agent, _, _ = env
    sess = build(env, [
        reply_tools(("rollout_restart", {"namespace": "demo", "name": "api",
                                         "rationale": "重启以恢复"})),
        reply_text("好了"),
    ])
    sess.send("重启")
    run(sess)
    sess.resolve_approval(sess.pending.proposal.proposal_id, True)
    run(sess)

    blob = json.loads(json.dumps(sess.to_dict(), default=str))
    revived = ChatSession.from_dict("s-x", blob, agent, ScriptedLLM([reply_text("x")]))
    assert revived.pending is None
    assert revived.status == "idle"
    # 但事件流里那张卡片还在——它是记录，不是待办
    kinds = [e.type for e in revived.events]
    assert "approval" in kinds and "decision" in kinds


# ---------------------------------------------------------------------------
# 「用文字征求同意」的触发器
#
# 这个 nudge 是为了修"A 说得很漂亮但什么都没发生"——模型把方案写在聊天里
# 而不是提卡片，运维人员没有东西可点。但它必须**收得很紧**，因为催模型
# "提点东西"是有代价的：踩过的坑是它被催之后，把自己上一段刚说过
# "不建议现在做"的 rollout_restart 提了出来，评测里 probe 场景直接从 ✓ 掉 ✗。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "你要走哪条？我提变更卡片给你确认。",
    "需要我提一个变更吗？",
    "你希望我直接提一个重启 api-gateway 的变更试试？",
    "要不要我现在执行？",
    "是否执行？请确认。",
    "要我直接改吗？",
    "要我动手吗",
    "你想走哪条路？",
    # ↓ 下面这几条是从真实评测结论里摘出来的。前两轮就是漏了它们，
    #   oom / cordon 明明推理出了正确答案却只是用文字问，nudge 没接住，
    #   结果一张卡片都没有，通过率从 11/15 掉到 10/15。
    "要我把内存上限改回 128Mi 吗？",
    "我会提一个变更卡片给你确认。",
    "我再提具体的变更卡片。",
    "如果你希望我先把 HPA 的副本区间调一下（比如临时把 min 提到 4 顶一下容量），"
    "我可以提变更卡片，但那是治标。",
    "这个我可以提变更卡片，但代价是服务只有单副本。",
])
def test_在征求执行同意时触发提醒(text):
    assert _is_asking_permission(text) is True


@pytest.mark.parametrize("text", [
    # 客套收尾：动词被"在你"隔开，不是在征求执行同意。
    # 这一条就是 probe 场景翻车的原话。
    "需要我在你改完探针之后，再帮你确认一次 Pod 是否稳定吗？",
    "我没有对应的工具，需要你手工执行 kubectl rollout undo。",
    "如果你知道实际挂的是哪个 ConfigMap，告诉我，我去比对配置。",
    "需要我给你一份排查清单吗？",
    # 「提变更卡片**给你**确认」是标准征求意见的说法，不能被当成客套误杀
    "需要我给你一份排查清单吗？",
    "要我帮你把节点列表整理出来吗？",
    "这个故障需要你手工执行，我没有对应工具。",
    "",
])
def test_客套收尾不该触发提醒(text):
    assert _is_asking_permission(text) is False


def test_提醒只发一次(env):
    """连续两轮都在文字里问，也只纠正一次——避免把模型拖进死循环。"""
    agent, _, _ = env
    sess = build(env, [
        reply_text("需要我提一个变更吗？"),
        reply_text("需要我提一个变更吗？"),
    ])
    sess.send("看看")
    run(sess)
    notes = [e for e in sess.events if e.type == "note"]
    assert len(notes) == 1
    assert sess.status == "idle"


# ---------------------------------------------------------------------------
# 退化循环护栏
#
# 评测里 crash 场景连续 21 次调 get_configmap 猜名字，把 12 轮预算烧光
# （10.6 万 token），最后连结论都没给出来。工具调用不消耗轮次预算是刻意的
# 设计（不惩罚多取证），但它没有配套防退化机制。
# ---------------------------------------------------------------------------


def test_连续落空会被点破(env):
    """crash 场景的真实形态：不是重复，是**换着名字猜**。

    只看"同参数重复"是抓不到的——每猜一个名字都是"新"调用。
    """
    agent, _, _ = env
    agent.k8s.readonly_result = "ConfigMap 不存在"     # 每次取证都落空

    script = [reply_tools(("get_configmap",
                           {"namespace": "demo", "name": f"guess-{i}"}))
              for i in range(6)]
    script.append(reply_text("用现有证据下结论"))
    sess = build(env, script)

    sess.send("api-gateway 崩了")
    run(sess)

    notes = [e.data["text"] for e in sess.events if e.type == "note"]
    assert any("原地打转" in t for t in notes), notes
    # 也要真的把系统消息塞进上下文，否则模型看不到
    assert any(m["role"] == "system" and "不要再用同样的方式查一遍" in m["content"]
               for m in sess.messages)


def test_同一组参数反复查会被点破(env):
    agent, _, _ = env
    agent.k8s.readonly_result = "pod 列表"

    script = [reply_tools(("get_pods", {"namespace": "demo"})) for _ in range(4)]
    script.append(reply_text("下结论"))
    sess = build(env, script)

    sess.send("看看")
    run(sess)

    notes = [e.data["text"] for e in sess.events if e.type == "note"]
    assert any("原地打转" in t for t in notes), notes


def test_提示只发一次不会刷屏(env):
    """点破一次就够。每轮反复注入会把上下文冲掉，反而更糟。"""
    agent, _, _ = env
    agent.k8s.readonly_result = "不存在"

    script = [reply_tools(("get_configmap", {"namespace": "demo", "name": f"g{i}"}))
              for i in range(10)]
    script.append(reply_text("下结论"))
    sess = build(env, script)

    sess.send("查")
    run(sess)

    notes = [e.data["text"] for e in sess.events if e.type == "note"]
    assert len([t for t in notes if "原地打转" in t]) == 1, notes


def test_正常取证不会被误伤(env):
    """最怕的是把正常排查误判成打转——那会让模型不敢多查。"""
    agent, _, _ = env
    agent.k8s.readonly_result = "有内容"

    script = [
        reply_tools(("get_pods", {"namespace": "demo"})),
        reply_tools(("get_logs", {"namespace": "demo", "pod": "a"})),
        reply_tools(("get_events", {"namespace": "demo"})),
        reply_tools(("get_workload", {"namespace": "demo", "name": "api-gateway"})),
        reply_tools(("get_metrics", {"namespace": "demo"})),
        reply_tools(("get_hpa", {"namespace": "demo", "name": "api-gateway"})),
        reply_text("查清楚了"),
    ]
    sess = build(env, script)

    sess.send("查一下")
    run(sess)

    notes = [e.data["text"] for e in sess.events if e.type == "note"]
    assert not any("原地打转" in t for t in notes), notes


def test_落空计数按工具隔离(env):
    """miss 计数按工具分开，但**不会被别的工具"洗掉"**。

    这个语义是刻意的：中间插一次 `get_pods` 并不能让"get_configmap 连查 6 次
    都没有"变得不像是打转。真正的重置条件是**同一个工具查到了东西**。
    """
    agent, _, _ = env
    agent.k8s.readonly_result = "不存在"

    script = [
        reply_tools(("get_configmap", {"namespace": "demo", "name": f"g{i}"}))
        for i in range(3)
    ] + [reply_tools(("get_pods", {"namespace": "demo"}))] + [
        reply_tools(("get_configmap", {"namespace": "demo", "name": f"h{i}"}))
        for i in range(3)
    ] + [reply_text("下结论")]
    sess = build(env, script)

    sess.send("查")
    run(sess)

    notes = [e.data["text"] for e in sess.events if e.type == "note"]
    assert any("原地打转" in t for t in notes), notes


def test_同一工具查到东西就重置落空计数(env):
    """真正的重置条件：同一个工具**成功**一次，之前的落空就不算了。"""
    agent, _, _ = env

    class _Flaky:
        """前 4 次说"不存在"，之后给内容。"""

        def __init__(self):
            self.n = 0

        def read_only_query(self, tool, params):
            self.n += 1
            return "ConfigMap 不存在" if self.n <= 4 else "DB_HOST=postgres"

    agent.k8s = _Flaky()
    script = [reply_tools(("get_configmap", {"namespace": "demo", "name": f"g{i}"}))
              for i in range(4)]
    script += [reply_tools(("get_configmap",
                            {"namespace": "demo", "name": "order-service-config"}))]
    script += [reply_tools(("get_configmap", {"namespace": "demo", "name": "x"}))]
    script.append(reply_text("下结论"))
    sess = build(env, script)

    sess.send("查")
    run(sess)

    notes = [e.data["text"] for e in sess.events if e.type == "note"]
    assert not any("原地打转" in t for t in notes), notes

