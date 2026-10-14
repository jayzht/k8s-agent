"""工具登记表的结构不变量。

这个文件的存在理由：**"能改但读不了"是一个结构性问题，不是一次疏忽。**
之前 `patch_hpa` 是写工具，却没有任何读 HPA 的只读工具，于是模型只能从事件里
猜 HPA 的状态，被一条无关的噪音事件带偏，给出了一份语气确定但根因错误的诊断。

一条测试守住它，比在代码评审里靠人记得更靠谱。
"""

from __future__ import annotations

import pytest

from omagent.tools import (
    FORBIDDEN,
    READONLY_TOOLS,
    TOOLS,
    LOCKED_WRITE_TOOLS,
    WRITE_TOOLS,
    is_mutating,
    openai_tool_schema,
)

# 每个写工具都必须有至少一个"对应的读工具"，否则模型就只能靠猜。
#
# 新增写工具时，如果忘了在下面登记，test_新增写工具必须想清楚怎么读 会失败——
# 这正是我们想要的：逼作者回答"它改的那个东西，模型读得到吗"。
READ_FOR_WRITE: dict[str, set[str]] = {
    "rollout_restart": {"get_workload", "get_replicasets"},
    "rollout_undo": {"get_workload", "get_replicasets"},
    "scale_workload": {"get_workload"},
    "delete_pod": {"get_pods"},
    "patch_resources": {"get_workload"},
    "patch_hpa": {"get_hpa"},
    "patch_hpa_target": {"get_hpa"},
    "cordon_node": {"get_nodes"},
    "uncordon_node": {"get_nodes"},
    "drain_node": {"get_nodes", "get_pdb"},
    "rollback_configmap": {"get_configmap"},
    # 配置类修复 —— 能改就必须能先看，否则等于让人闭着眼睛改
    "patch_service_selector": {"get_services", "get_endpoints"},
    "patch_service_port": {"get_services"},
    "patch_probe": {"get_workload"},
    "patch_node_selector": {"get_workload", "get_nodes"},
    # 默认锁着的禁区工具。**解锁不等于不需要读**——恰恰相反：
    # 越是不可逆的动作，越要保证模型能先把对象读清楚再动手。
    "delete_pvc": {"get_pvc"},
}


# ── 读写对称性 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("write_tool", (*WRITE_TOOLS, *LOCKED_WRITE_TOOLS))
def test_每个写工具都有对应的读工具(write_tool):
    readers = READ_FOR_WRITE.get(write_tool, set())
    assert readers, f"{write_tool} 没有登记对应的读工具"
    for r in readers:
        assert r in TOOLS, f"{write_tool} 声明的读工具 {r} 不存在"
        assert r in READONLY_TOOLS, f"{write_tool} 声明的 {r} 不是只读工具"


def test_新增写工具必须想清楚怎么读():
    """防止有人加了写工具却忘了登记读工具。"""
    missing = (set(WRITE_TOOLS) | set(LOCKED_WRITE_TOOLS)) - set(READ_FOR_WRITE)
    assert not missing, (
        f"这些写工具没登记读工具：{sorted(missing)}。"
        f"请先确认模型能不能读到它改的那个对象，再补进 READ_FOR_WRITE。"
    )


def test_登记表里没有多余的项():
    extra = set(READ_FOR_WRITE) - set(WRITE_TOOLS) - set(LOCKED_WRITE_TOOLS)
    assert not extra, f"READ_FOR_WRITE 里有已经不存在的写工具：{sorted(extra)}"


# ── 基本一致性 ────────────────────────────────────────────────────────────


def test_每工具必属且仅属一类():
    assert set(READONLY_TOOLS) | set(WRITE_TOOLS) == set(TOOLS)
    assert not (set(READONLY_TOOLS) & set(WRITE_TOOLS))


def test_工具不能在登记表里同时是被禁止的():
    assert not (set(TOOLS) & set(FORBIDDEN))


def test_模型看不到任何禁止动作():
    names = {t["function"]["name"] for t in openai_tool_schema()}
    assert names == set(TOOLS)
    assert not (names & set(FORBIDDEN))


def test_每个工具都有参数schema且必需字段在properties里():
    for spec in TOOLS.values():
        assert spec.schema.get("type") == "object", spec.name
        props = set(spec.schema.get("properties", {}))
        missing = [r for r in spec.schema.get("required", []) if r not in props]
        assert not missing, f"{spec.name} 的 required 里有未声明的字段：{missing}"


def test_每个写工具都强制要求写理由():
    from omagent.tools import RATIONALE_FIELD

    for name in WRITE_TOOLS:
        spec = TOOLS[name]
        assert RATIONALE_FIELD in spec.params, f"{name} 缺少 rationale 字段"
        assert RATIONALE_FIELD in spec.required, f"{name} 的 rationale 不是必填"

    for name in READONLY_TOOLS:
        assert RATIONALE_FIELD not in TOOLS[name].params, f"只读工具 {name} 不该要理由"


def test_未登记工具默认当写操作():
    assert is_mutating("rollout_restart") is True
    assert is_mutating("get_pods") is False
    assert is_mutating("some_tool_that_does_not_exist") is True


def test_每个写工具的参数里都有命名空间或节点():
    """写操作必须能定位到具体对象，否则护栏无从下手。"""
    for name in WRITE_TOOLS:
        props = TOOLS[name].params
        assert "namespace" in props or "node" in props, f"{name} 没有 namespace/node 参数"


def test_案例检索是只读工具():
    """`search_cases` 查的是历史记录，不改任何东西，所以必须是只读、自动放行。"""
    from omagent.tools import TOOLS

    assert "search_cases" in TOOLS
    assert TOOLS["search_cases"].mutating is False


# ---------------------------------------------------------------------------
# 序列化白名单：加了字段别忘了让它过河
#
# 这个坑在同一个项目里踩了三次（HTTP 头、provider tool、这里的 danger）：
# 给 dataclass 加了字段，但 hand-written 的 to_dict() 是**显式列举**的，
# 新字段悄悄丢在路上——而且不报错。
# 所以用一个通用断言把这条钉死，以后加字段会自动被发现。
# ---------------------------------------------------------------------------


def test_模型的每个字段都过得去_to_dict():
    """加了字段别忘了让它过河。

    这个坑在同一个项目里踩了三次（HTTP 头、provider tool、这里的 danger）：
    给 dataclass 加了字段，但 hand-written 的 to_dict() 是**显式列举**的，
    新字段悄悄丢在路上——**而且不报错**。

    最典型的后果：审批卡片拿不到 danger，高风险操作长得和普通操作一模一样。
    """
    import dataclasses
    from omagent.models import Proposal, Target

    prop = Proposal(tool="rollout_restart", params={},
                    target=Target(kind="Deployment", namespace="demo", name="x"))
    dumped = prop.to_dict()
    for f in dataclasses.fields(Proposal):
        assert f.name in dumped, (
            f"Proposal.{f.name} 不在 to_dict() 里——"
            f"hand-written 的序列化白名单漏了新字段，前端会拿不到它"
        )

    # 反向也要能回来
    back = Proposal.from_dict(dumped)
    assert back.danger == prop.danger
    assert back.tool == prop.tool


# ── 确认卡片上的"目标"必须是准的 ──────────────────────────────────────────
#
# 确认卡片是这套系统里**唯一让人做决定的地方**。卡片上的目标写错了，
# 人就是对着错误的信息点的批准——那比没有卡片更糟，因为他以为自己看过了。
#
# 这里踩过一次，而且是一次核对里同时暴露 4 个：
#   delete_pvc            被显示成 Deployment/xxx（明明是 PVC）
#   patch_hpa_target      显示成 Deployment/（名字是空的）
#   patch_service_selector 显示成 Deployment/（空，它用的是 service 参数）
#   patch_service_port     同上
# 根因是 target_of 用 if 链 + 兜底，任何没被 if 命中的工具都被默默当成 Deployment。


# 每个写工具一组"能真正跑通"的参数，用来驱动目标解析。
# 故意**不**从 schema 自动生成：自动生成的参数只能证明"不报错"，
# 证明不了"解析出来的目标是这个工具真正作用的那个对象"。
TARGET_PARAMS: dict[str, dict] = {
    "rollout_restart": {"namespace": "demo", "name": "api-gateway"},
    "rollout_undo": {"namespace": "demo", "name": "api-gateway"},
    "scale_workload": {"namespace": "demo", "name": "api-gateway", "replicas": 3},
    "delete_pod": {"namespace": "demo", "pod": "api-gateway-0"},
    "patch_resources": {"namespace": "demo", "name": "api-gateway"},
    "patch_hpa": {"namespace": "demo", "hpa": "api-gateway"},
    "patch_hpa_target": {"namespace": "demo", "hpa": "api-gateway", "target": "api-gateway"},
    "cordon_node": {"node": "om-sandbox-worker"},
    "uncordon_node": {"node": "om-sandbox-worker"},
    "drain_node": {"node": "om-sandbox-worker", "namespace": "demo"},
    "rollback_configmap": {"namespace": "demo", "name": "app-config"},
    "patch_service_selector": {"namespace": "demo", "service": "order-service",
                               "selector": {"app": "order-service"}},
    "patch_service_port": {"namespace": "demo", "service": "order-service", "target_port": 8080},
    "patch_probe": {"namespace": "demo", "name": "api-gateway", "port": 8080},
    "patch_node_selector": {"namespace": "demo", "name": "api-gateway"},
    "delete_pvc": {"namespace": "demo", "name": "scratch-migration-temp"},
}

# 明确期望的资源类型。**故意手写**，不是从代码里推出来的——
# 从被测代码推出期望值，等于用同一份错误去验证自己。
EXPECTED_KIND: dict[str, str] = {
    "delete_pod": "Pod",
    "delete_pvc": "PersistentVolumeClaim",
    "rollback_configmap": "ConfigMap",
    "patch_hpa": "HorizontalPodAutoscaler",
    "patch_hpa_target": "HorizontalPodAutoscaler",
    "patch_service_selector": "Service",
    "patch_service_port": "Service",
    "cordon_node": "Node",
    "uncordon_node": "Node",
    "drain_node": "Node",
    "rollout_restart": "Deployment",
    "rollout_undo": "Deployment",
    "scale_workload": "Deployment",
    "patch_resources": "Deployment",
    "patch_probe": "Deployment",
    "patch_node_selector": "Deployment",
}


def test_每个写工具的参数样例都齐():
    """TARGET_PARAMS 自己也得被守着，否则下面那条测试会因为漏项而"静默通过"。"""
    from omagent.tools import LOCKED_WRITE_TOOLS

    missing = (set(WRITE_TOOLS) | set(LOCKED_WRITE_TOOLS)) - set(TARGET_PARAMS)
    assert not missing, f"这些写工具没有目标解析的样例参数：{sorted(missing)}"


@pytest.mark.parametrize("tool", (*WRITE_TOOLS, *LOCKED_WRITE_TOOLS))
def test_确认卡片上的目标必须非空且类型正确(tool):
    from omagent.agent import OpsAgent
    from omagent.tools import LOCKED_SPECS

    spec = TOOLS.get(tool) or LOCKED_SPECS[tool]
    t = OpsAgent.target_of(spec, TARGET_PARAMS[tool])

    assert t.name, (
        f"{tool} 的确认卡片上**目标是空的**——运维只看到「{t.kind}/ 」，"
        f"根本不知道这个动作会作用在谁身上"
    )
    assert t.kind == EXPECTED_KIND[tool], (
        f"{tool} 的卡片把目标类型显示成 {t.kind}，应该是 {EXPECTED_KIND[tool]}"
    )


def test_节点级动作不带命名空间():
    """节点是集群级对象，卡片上写"命名空间 demo 的节点"会让人以为只影响那个命名空间。"""
    from omagent.agent import OpsAgent

    for tool in ("cordon_node", "uncordon_node", "drain_node"):
        t = OpsAgent.target_of(TOOLS[tool], TARGET_PARAMS[tool])
        assert t.kind == "Node" and t.namespace == ""
