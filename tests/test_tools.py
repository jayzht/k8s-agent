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
    "cordon_node": {"get_nodes"},
    "uncordon_node": {"get_nodes"},
    "drain_node": {"get_nodes", "get_pdb"},
    "rollback_configmap": {"get_configmap"},
}


# ── 读写对称性 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("write_tool", WRITE_TOOLS)
def test_每个写工具都有对应的读工具(write_tool):
    readers = READ_FOR_WRITE.get(write_tool, set())
    assert readers, f"{write_tool} 没有登记对应的读工具"
    for r in readers:
        assert r in TOOLS, f"{write_tool} 声明的读工具 {r} 不存在"
        assert r in READONLY_TOOLS, f"{write_tool} 声明的 {r} 不是只读工具"


def test_新增写工具必须想清楚怎么读():
    """防止有人加了写工具却忘了登记读工具。"""
    missing = set(WRITE_TOOLS) - set(READ_FOR_WRITE)
    assert not missing, (
        f"这些写工具没登记读工具：{sorted(missing)}。"
        f"请先确认模型能不能读到它改的那个对象，再补进 READ_FOR_WRITE。"
    )


def test_登记表里没有多余的项():
    extra = set(READ_FOR_WRITE) - set(WRITE_TOOLS)
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
