"""工具登记表——**整个产品唯一的安全切分点**。

每个工具在这里声明一次，声明两件事：

1. ``mutating``：它会不会改变集群状态。
2. ``schema``：它的参数长什么样（直接作为 function-calling 的 JSON Schema 发给模型）。

``mutating`` 不是提示词里的建议，也不是配置文件里的策略，而是**代码里的常量**。
执行路径按它分流：

- ``mutating=False`` → ``OpsAgent.run_readonly()`` 自动执行，模型可以连续调用任意多轮。
- ``mutating=True``  → 只能经由 ``OpsAgent.execute_write()``，而它要求一个
  ``approved=True`` 且 ``proposal_id`` 严格匹配的 ``Decision``。

于是"只读自动跑、写入必须问人"这件事不依赖模型自觉，也不依赖调用方自觉——
它在代码路径上就是两条路。

**禁止动作**（``FORBIDDEN``）的做法是：根本不出现在工具清单里，模型看不见也就调不到；
即使它凭空编出一个名字，``execute_write()`` 也会在查表阶段直接拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mutating: bool
    summary: str  # 一句话人话说明，给操作员看
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def params(self) -> dict[str, Any]:
        return self.schema.get("properties", {})

    @property
    def required(self) -> list[str]:
        return list(self.schema.get("required", []))


_NS = {"type": "string", "description": "命名空间"}
_KIND = {
    "type": "string",
    "enum": ["deployment", "statefulset"],
    "description": "工作负载类型，默认 deployment",
}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# 每个写工具都必须回答"为什么"。这不是可选的装饰——确认卡片上没有理由，
# 运维人员就只剩"信不信这个模型"这一个判断依据，而那正是最不该依赖的东西。
# 把它设成 required，模型就必须先想清楚再提方案。
RATIONALE_FIELD = "rationale"
_RATIONALE_PROP = {
    "type": "string",
    "description": (
        "为什么做这个改动、预期结果是什么、有什么风险。"
        "这段话会原样显示在确认卡片上给运维人员看，请写具体，不要写套话。"
    ),
}


def _wobj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """写工具的 schema：自动带上必填的 rationale。"""
    return _obj({**props, RATIONALE_FIELD: _RATIONALE_PROP}, [*required, RATIONALE_FIELD])


# --- 只读工具：自动执行 ------------------------------------------------------

_READONLY: list[ToolSpec] = [
    ToolSpec(
        "get_pods", False, "列出 Pod 及其状态、重启次数、退出码、内存上限",
        _obj({"namespace": _NS, "label_selector": {"type": "string", "description": "标签选择器，如 app=api-gateway"}}, ["namespace"]),
    ),
    ToolSpec(
        "get_events", False, "读取命名空间（或指定对象）的事件，看 K8s 自己怎么说的",
        _obj({"namespace": _NS, "name": {"type": "string", "description": "只看与该对象相关的事件"}}, ["namespace"]),
    ),
    ToolSpec(
        "get_logs", False, "读取 Pod 日志；previous=true 读上一个已崩溃实例的日志",
        _obj(
            {
                "namespace": _NS,
                "pod": {"type": "string", "description": "Pod 名"},
                "container": {"type": "string", "description": "容器名，多容器时必填"},
                "tail_lines": {"type": "integer", "description": "读取末尾行数，默认 40"},
                "previous": {"type": "boolean", "description": "读上一个实例（崩溃现场）"},
            },
            ["namespace", "pod"],
        ),
    ),
    ToolSpec(
        "get_workload", False, "读取工作负载规格：副本数、镜像、资源、调度约束",
        _obj({"namespace": _NS, "name": {"type": "string"}, "kind": _KIND}, ["namespace", "name"]),
    ),
    ToolSpec(
        "get_nodes", False, "列出节点及其可调度状态",
        _obj({}, []),
    ),
    ToolSpec(
        "get_endpoints", False, "查询 Service 的后端端点，判断有没有可用后端",
        _obj({"namespace": _NS, "name": {"type": "string", "description": "Service 名，省略则列全部"}}, ["namespace"]),
    ),
    ToolSpec(
        "get_services", False, "列出 Service 及其端口映射，用于和配置交叉比对",
        _obj({"namespace": _NS}, ["namespace"]),
    ),
    ToolSpec(
        "get_pdb", False, "读取 PodDisruptionBudget，判断能不能安全驱逐",
        _obj({"namespace": _NS}, ["namespace"]),
    ),
    ToolSpec(
        "get_configmap", False, "读取 ConfigMap 内容（只读，永不接触 Secret）",
        _obj({"namespace": _NS, "name": {"type": "string"}}, ["namespace", "name"]),
    ),
    # 下面四个是补上的。原则：**每个写工具都必须有一个对应的读工具。**
    # 缺了它们就会出现"改得了一个自己读不了的对象"——模型只能从事件里猜，
    # 而集群里任何一条沾边的噪音事件都会把它带偏（真发生过：问"HPA 坏了吗"，
    # 它把"没装 metrics-server"这条一直存在的环境噪音当成了刚发生的根因）。
    ToolSpec(
        "get_hpa", False,
        "读取 HPA 的副本区间、当前/期望副本数与状态条件（扩缩容是否失效看这里）",
        _obj({"namespace": _NS,
              "name": {"type": "string", "description": "HPA 名，省略则列全部"}},
             ["namespace"]),
    ),
    ToolSpec(
        "get_resourcequota", False,
        "读取 ResourceQuota 的硬上限与已用量（判断是否配额用满）",
        _obj({"namespace": _NS}, ["namespace"]),
    ),
    ToolSpec(
        "get_replicasets", False,
        "列出 ReplicaSet 与版本（回滚前先看有几个历史版本）",
        _obj({"namespace": _NS,
              "name": {"type": "string", "description": "只看属于该 Deployment 的 ReplicaSet"}},
             ["namespace"]),
    ),
    ToolSpec(
        "get_pvc", False, "列出 PVC 的绑定状态与容量（有状态服务出问题必看）",
        _obj({"namespace": _NS, "name": {"type": "string"}}, ["namespace"]),
    ),
    ToolSpec(
        "get_metrics", False,
        "读取资源的实际用量：谁在吃内存、离上限还有多远、节点利用率",
        _obj({"namespace": _NS}, ["namespace"]),
    ),
    ToolSpec(
        "search_cases", False,
        "查历史案例：同样的症状以前处理过吗、当时怎么修的、结果如何",
        _obj({"namespace": _NS, "name": {"type": "string"},
              "kind": {"type": "string", "description": "工作负载类型，默认 deployment"}},
             ["namespace", "name"]),
    ),
]

# --- 写工具：必须人工批准 ----------------------------------------------------

_WRITE: list[ToolSpec] = [
    ToolSpec(
        "rollout_restart", True, "滚动重启工作负载（逐个替换实例，不中断服务）",
        _wobj({"namespace": _NS, "name": {"type": "string"}, "kind": _KIND}, ["namespace", "name"]),
    ),
    ToolSpec(
        "rollout_undo", True, "回滚到上一个版本",
        _wobj({"namespace": _NS, "name": {"type": "string"}}, ["namespace", "name"]),
    ),
    ToolSpec(
        "scale_workload", True, "调整副本数",
        _wobj(
            {"namespace": _NS, "name": {"type": "string"}, "kind": _KIND,
             "replicas": {"type": "integer", "description": "目标副本数"}},
            ["namespace", "name", "replicas"],
        ),
    ),
    ToolSpec(
        "delete_pod", True, "删除单个 Pod 触发重建（比重启更快，但会短暂少一个实例）",
        _wobj({"namespace": _NS, "pod": {"type": "string"}}, ["namespace", "pod"]),
    ),
    ToolSpec(
        "patch_resources", True, "调整容器的 requests/limits（改内存上限用这个）",
        _wobj(
            {
                "namespace": _NS,
                "name": {"type": "string"},
                "container": {"type": "string", "description": "容器名"},
                "memory_limit": {"type": "string", "description": "如 512Mi"},
                "cpu_limit": {"type": "string", "description": "如 500m"},
                "memory_request": {"type": "string"},
                "cpu_request": {"type": "string"},
            },
            ["namespace", "name", "container"],
        ),
    ),
    ToolSpec(
        "patch_hpa", True, "调整 HPA 的副本区间",
        _wobj(
            {"namespace": _NS, "hpa": {"type": "string"},
             "min_replicas": {"type": "integer"}, "max_replicas": {"type": "integer"}},
            ["namespace", "hpa"],
        ),
    ),
    ToolSpec(
        "cordon_node", True, "标记节点不可调度（不影响已在运行的 Pod）",
        _wobj({"node": {"type": "string"}}, ["node"]),
    ),
    ToolSpec(
        "uncordon_node", True, "恢复节点可调度（cordon 的回滚动作）",
        _wobj({"node": {"type": "string"}}, ["node"]),
    ),
    ToolSpec(
        "drain_node", True, "驱逐节点上的 Pod（会逐个迁移，PDB 不满足时会被拒绝）",
        _wobj(
            {"node": {"type": "string"},
             "ignore_daemonsets": {"type": "boolean", "description": "忽略 DaemonSet 管理的 Pod（不会删除它们）"},
             "delete_emptydir_data": {"type": "boolean", "description": "允许驱逐使用 emptyDir 的 Pod，其数据会丢失"}},
            ["node"],
        ),
    ),
    ToolSpec(
        "rollback_configmap", True, "把 ConfigMap 回滚到指定内容",
        _wobj(
            {"namespace": _NS, "name": {"type": "string"},
             "data": {"type": "object", "description": "要恢复的完整键值对", "additionalProperties": {"type": "string"}}},
            ["namespace", "name", "data"],
        ),
    ),
]

TOOLS: dict[str, ToolSpec] = {t.name: t for t in (*_READONLY, *_WRITE)}

READONLY_TOOLS: tuple[str, ...] = tuple(t.name for t in _READONLY)
WRITE_TOOLS: tuple[str, ...] = tuple(t.name for t in _WRITE)

# ---------------------------------------------------------------------------
# 禁止动作
#
# 这些**不进工具清单**——模型看不见，因此不会去调。
# 列在这里是为了第二条防线：万一名字从别处冒出来（旧会话、手搓请求、模型幻觉），
# 执行器能明确地拒绝它，而不是报"未知工具"。
# ---------------------------------------------------------------------------
FORBIDDEN: dict[str, str] = {
    "delete_workload": "删除工作负载。运维 Agent 不做删除，请走变更流程。",
    "delete_namespace": "删除命名空间。这是不可逆操作，任何 Agent 都不该有这条路。",
    "delete_pvc": "删除 PVC。会丢数据。",
    "delete_pv": "删除 PV。会丢数据。",
    "modify_rbac": "修改 RBAC。这是提权路径，必须由人手工执行。",
    "read_secret": "读取 Secret。Agent 全程不接触 Secret 内容。",
    "exec_in_pod": "在 Pod 内执行任意命令。这等于把集群交给模型自由发挥。",
    "apply_manifest": "应用任意 YAML。参数化工具之外的口子一律不开。",
}


def is_mutating(name: str) -> bool:
    """未登记的工具**默认是写操作**（fail closed）。

    宁可误拦一个只读动作，也不能把一个没声明清楚的工具当成只读放行。
    """
    spec = TOOLS.get(name)
    return True if spec is None else spec.mutating


def openai_tool_schema() -> list[dict[str, Any]]:
    """把工具清单转成 OpenAI/DeepSeek 的 function-calling 格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": (
                    f"{spec.summary}"
                    + ("" if not spec.mutating else "（写操作：调用后不会立即执行，需要运维人员确认）")
                ),
                "parameters": spec.schema,
            },
        }
        for spec in TOOLS.values()
    ]


def ui_tool_list() -> list[dict[str, Any]]:
    """给前端「工具能力」面板用的清单。"""
    return [
        {"name": s.name, "mutating": s.mutating, "summary": s.summary}
        for s in TOOLS.values()
    ]
