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
import threading

# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mutating: bool
    summary: str  # 一句话人话说明，给操作员看
    schema: dict[str, Any] = field(default_factory=dict)
    # 危险级别：决定审批卡片长什么样，以及**要不要额外的摩擦**。
    #
    #   "normal"  —— 可逆、单个对象。卡片照常
    #   "caution" —— 不可逆（虽然能被控制器/人工恢复）。卡片加醒目警告
    #   "severe"  —— 不可逆且可能丢数据，或突破授权边界。
    #                **必须手输目标名字才能批准**——见下面为什么
    #
    # 为什么 severe 不能只是一条红色横幅：**被动警告会脱敏**。
    # 浏览器"此网站不安全"、Windows UAC、SSL 证书警告，全都是被点顺手的。
    # 真正拦得住的是**摩擦**：让人做一件肌肉记忆做不到的事。
    # （GitHub 删仓库、改 package 可见性都是让你手输名字——这是同一个道理。）
    danger: str = "normal"
    # 这次操作是否受"写操作命名空间白名单"约束。
    #
    # **默认 True（fail closed）**：新增的工具默认要过白名单，除非它显式说明
    # 自己不针对某个命名空间（比如节点级动作、或者调外部系统的工具）。
    # 以前这里是一句 `if spec.name in ("cordon_node", "uncordon_node", "drain_node")`
    # ——硬编码工具名做例外，加了 provider 之后根本不会跟着扩展。
    namespace_scoped: bool = True

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
        "get_capacity", False,
        "集群余量：每个节点可分配 vs 已预留 vs 实际使用，还剩多少可调度，"
        "以及配额使用率和预留最多的工作负载（回答\"新服务还部署得下吗\"）",
        _obj({"namespace": {**_NS, "description": "只影响配额与\"预留最多\"两段，"
                                                "节点余量始终按全集群算"}},
             ["namespace"]),
    ),
    ToolSpec(
        "get_cost", False,
        "按价格表估算成本：预留花销、按命名空间/工作负载拆分，"
        "以及**预留了但没用**的闲置成本（那部分才是能省的）",
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
        "rollout_history", False,
        "列出某个工作负载的可回滚版本（revision、镜像、副本数、创建时间、哪个是当前版本），"
        "回滚到指定版本前先看它",
        _obj({"namespace": _NS, "name": {"type": "string"}, "kind": _KIND}, ["namespace", "name"]),
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
        # 不可逆：dry-run 对删除无效（实测 delete --dry-run=server 会通过），
        # 所以卡片的措辞要更硬，别让人以为还能撤销。
        danger="caution",
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
    # ── 配置类修复：在此之前，4 个评测场景的正解都是"我没有工具" ──
    ToolSpec(
        "patch_service_selector", True,
        "改 Service 的 selector（Pod 健康但流量送不到时用这个；⚠️ 选错会让流量全断）",
        _wobj({"namespace": _NS, "service": {"type": "string", "description": "Service 名"},
               "selector": {"type": "object", "description": "新的标签选择器，如 {app: web}"}},
              ["namespace", "service", "selector"]),
        danger="caution",
    ),
    ToolSpec(
        "patch_service_port", True, "改 Service 的 targetPort（连接被拒、端口对不上时用）",
        _wobj({"namespace": _NS, "service": {"type": "string"},
               "port_name": {"type": "string", "description": "按端口名定位，二选一"},
               "port": {"type": "integer", "description": "按 service port 号定位，二选一"},
               "target_port": {"type": "integer", "description": "要改成的容器端口"}},
              ["namespace", "service", "target_port"]),
        danger="caution",
    ),
    ToolSpec(
        "patch_probe", True, "改容器的探针端口（探针打错端口导致反复重启时用）",
        _wobj({"namespace": _NS, "name": {"type": "string"}, "kind": _KIND,
               "container": {"type": "string", "description": "容器名，默认 app"},
               "probe": {"type": "string", "enum": ["liveness", "readiness", "startup"],
                         "description": "改哪个探针，默认 liveness"},
               "port": {"type": "integer", "description": "要改成的端口"}},
              ["namespace", "name", "port"]),
        danger="caution",
    ),
    ToolSpec(
        "patch_node_selector", True,
        "增删 Deployment 的 nodeSelector（Pod 调度不上去时用；**能删键**）",
        _wobj({"namespace": _NS, "name": {"type": "string"}, "kind": _KIND,
               "selector": {"type": "object", "description": "要新增/覆盖的标签"},
               "remove": {"type": "array", "items": {"type": "string"},
                          "description": "要**删除**的键名列表（merge patch 删不掉，这里走 JSON patch）"}},
              ["namespace", "name"]),
        danger="caution",
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
        "patch_hpa_target", True,
        "改 HPA 的扩缩容目标（scaleTargetRef 被指到不存在的负载、"
        "副本数完全不动时用；⚠️ 指错对象会给别的负载扩缩容）",
        _wobj({"namespace": _NS, "hpa": {"type": "string", "description": "HPA 名"},
               "target": {"type": "string", "description": "要指向的工作负载名"},
               "kind": {"type": "string", "description": "默认 Deployment"},
               "api_version": {"type": "string", "description": "默认 apps/v1"}},
              ["namespace", "hpa", "target"]),
        danger="caution",
    ),
    ToolSpec(
        "patch_resourcequota", True,
        "改 ResourceQuota 的上限（Pod 因配额用满起不来时用；"
        "⚠️ 调小到低于当前用量不会回收任何东西，只会让新 Pod 全起不来）",
        _wobj({"namespace": _NS, "name": {"type": "string", "description": "ResourceQuota 名"},
               "hard": {"type": "object", "description": "要设的上限，如 {\"requests.cpu\": \"8\"}"},
               "remove": {"type": "array", "items": {"type": "string"},
                          "description": "要删除的配额项（merge patch 删不掉，这里走 JSON patch）"}},
              ["namespace", "name"]),
        danger="caution",
    ),
    ToolSpec(
        "patch_pdb", True,
        "改 PodDisruptionBudget（排空节点被 PDB 拦住时用；"
        "⚠️ 调松会让排空/升级期间可同时中断的副本变多）",
        _wobj({"namespace": _NS, "name": {"type": "string", "description": "PDB 名"},
               "min_available": {"type": "string",
                                 "description": "最少可用，数字或百分比，如 2 或 \"50%\""},
               "max_unavailable": {"type": "string",
                                   "description": "最多不可用，与 min_available 互斥"},
               "remove": {"type": "array", "items": {"type": "string"},
                          "description": "要删掉的字段名，如 [\"minAvailable\"]"}},
              ["namespace", "name"]),
        danger="caution",
    ),
    ToolSpec(
        "cordon_node", True, "标记节点不可调度（不影响已在运行的 Pod）",
        _wobj({"node": {"type": "string"}}, ["node"]),
        namespace_scoped=False,   # 节点级动作，没有命名空间这回事
    ),
    ToolSpec(
        "uncordon_node", True, "恢复节点可调度（cordon 的回滚动作）",
        _wobj({"node": {"type": "string"}}, ["node"]),
        namespace_scoped=False,
    ),
    ToolSpec(
        "drain_node", True, "驱逐节点上的 Pod（会逐个迁移，PDB 不满足时会被拒绝）",
        _wobj(
            {"node": {"type": "string"},
             "ignore_daemonsets": {"type": "boolean", "description": "忽略 DaemonSet 管理的 Pod（不会删除它们）"},
             "delete_emptydir_data": {"type": "boolean", "description": "允许驱逐使用 emptyDir 的 Pod，其数据会丢失"}},
            ["node"],
        ),
        namespace_scoped=False,
        # 会驱逐节点上**所有** Pod（除非 PDB 拦住），影响面比删一个 Pod 大得多。
        danger="caution",
    ),
    ToolSpec(
        "rollback_configmap", True, "把 ConfigMap 回滚到指定内容",
        _wobj(
            {"namespace": _NS, "name": {"type": "string"},
             "data": {"type": "object", "description": "要恢复的完整键值对", "additionalProperties": {"type": "string"}}},
            ["namespace", "name", "data"],
        ),
    ),
    # ── 变更管理：发布 / 回滚到指定版本 / 金丝雀灰度 ──────────────────────
    #
    # 这一组不是"修坏掉的东西"，而是"安全地换版本"。之所以不做成一个万能的
    # patch/apply_manifest：可控的变更管理靠的恰恰是**把常见动作拆成窄工具**。
    # 参数化边界（比如金丝雀名字固定、kind 只能是 deployment）就是在这里划出来的。
    ToolSpec(
        "patch_image", True,
        "换指定容器的镜像（发布新版本用这个）；改完会读回确认是否真的生效，"
        "要回滚用 rollout_history 查版本、再 rollout_undo_to",
        _wobj(
            {"namespace": _NS, "name": {"type": "string"},
             "kind": _KIND,
             "container": {"type": "string",
                           "description": "容器名，省略则用名为 app 的容器（没有就用第一个）"},
             "image": {"type": "string", "description": "新镜像，如 registry.example.com/app:v2"}},
            ["namespace", "name", "image"],
        ),
        danger="caution",
    ),
    ToolSpec(
        "patch_configmap", True,
        "改 ConfigMap 的键值（可以新增/覆盖，也可以删键）；"
        "改完**不会自动重启 Pod**，要生效还要配合 rollout_restart",
        _wobj(
            {"namespace": _NS, "name": {"type": "string", "description": "ConfigMap 名"},
             "data": {"type": "object", "description": "要新增/覆盖的键值",
                      "additionalProperties": {"type": "string"}},
             "remove": {"type": "array", "items": {"type": "string"},
                        "description": "要**删除**的键名列表（merge patch 删不掉 map 里的键，"
                                       "带它时走 JSON patch）"}},
            ["namespace", "name"],
        ),
        danger="caution",
    ),
    ToolSpec(
        "rollout_undo_to", True,
        "回滚到指定版本（revision 用 rollout_history 查）；"
        "merge patch 滚不干净的字段会被如实报出来",
        _wobj(
            {"namespace": _NS, "name": {"type": "string"}, "kind": _KIND,
             "revision": {"type": "integer",
                          "description": "要回滚到的版本号，来自 rollout_history"}},
            ["namespace", "name", "revision"],
        ),
        danger="caution",
    ),
    ToolSpec(
        "canary_start", True,
        "起金丝雀：把稳定版 Deployment 克隆成 <name>-canary 并只改镜像，"
        "两个版本共用同一套标签，流量按**就绪副本数比例**灰度",
        _wobj(
            {"namespace": _NS, "name": {"type": "string", "description": "稳定版 Deployment 名"},
             # 灰度是 Deployment 之间的对等关系，StatefulSet 没有"克隆一份"的语义
             "kind": {"type": "string", "enum": ["deployment"], "description": "目前只支持 deployment"},
             "image": {"type": "string", "description": "金丝雀版本要跑的镜像"},
             "container": {"type": "string", "description": "改哪个容器的镜像，默认 app"},
             "replicas": {"type": "integer", "description": "金丝雀副本数，至少 1"}},
            ["namespace", "name", "image", "replicas"],
        ),
        danger="caution",
    ),
    ToolSpec(
        "canary_promote", True,
        "金丝雀转正：把 <name>-canary 的镜像发布到稳定版，并把金丝雀缩到 0 副本",
        _wobj(
            {"namespace": _NS, "name": {"type": "string", "description": "稳定版 Deployment 名"}},
            ["namespace", "name"],
        ),
        danger="caution",
    ),
    ToolSpec(
        "canary_abort", True,
        "放弃金丝雀：把 <name>-canary 缩到 0 副本，稳定版一个字都不动",
        _wobj(
            {"namespace": _NS, "name": {"type": "string", "description": "稳定版 Deployment 名"}},
            ["namespace", "name"],
        ),
        danger="normal",
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


# ---------------------------------------------------------------------------
# 默认锁着、但**可以显式解锁**的禁区工具
# ---------------------------------------------------------------------------
#
# 这一段是被运维人员问出来的。原来的做法是**一律禁止**：上面 8 个动作
# 永远不可用。但那个反驳是对的——**最终都要人来审批**，一刀切只会让系统
# 变窄：遇到"这个 PVC 就是上次删工作负载留下的残留，删了没事"的时候，
# Agent 只能干看着，人还是得自己开终端。
#
# 所以改成**默认锁着 + 启动时显式解锁**：
#
#   - 默认行为**一个字没改**：不在工具清单里，模型看不见、调不到；
#   - 运维用 `--allow-tool <名字>` 在启动时单独打开某一个；
#   - 能打开哪些由**代码**决定（`UNLOCKABLE`），不是一个随手能改的开关：
#     每个可解锁的工具都必须自带**能用的 dry-run** 和参数化实现。
#
# 为什么不做一个 `--unsafe` 全开：那等于把 8 个决定合并成 1 个，而它们的
# 风险根本不是一回事（读 Secret 和删命名空间差着量级）。一个名字一个开关，
# 运维得为每一个**单独**做决定，审计里也看得出来他到底开了哪个。
#
# 为什么解锁出来的都标 `severe`：能进这个列表的基本都不可逆，而
# **被动警告会脱敏**——红色横幅点几次就顺手了。真正拦得住的是**摩擦**：
# 让人手输目标名字。GitHub 删仓库、改 package 可见性用的是同一个办法。

LOCKED_WRITE: tuple[ToolSpec, ...] = (
    ToolSpec(
        "delete_pvc", True,
        # ⚠️ 这里**只描述这个动作做什么**，不要写"默认锁着、需要 --allow-tool"。
        # 踩过的坑：写了那句之后，模型读到的是"这个工具现在是锁着的"，
        # 于是它明明看得见也不提卡片，还会回一句"我没有删除类工具"。
        # 解锁方式属于**给运维看的**信息，放在 --allow-tool 的帮助和文档里。
        "删除一个 PVC（⚠️ 不可逆、会丢数据；服务端 dry-run 对删除不提供保护）",
        _wobj({"namespace": _NS, "name": {"type": "string", "description": "PVC 名"}},
              ["namespace", "name"]),
        danger="severe",
    ),
)

LOCKED_SPECS: dict[str, ToolSpec] = {t.name: t for t in LOCKED_WRITE}
LOCKED_WRITE_TOOLS: tuple[str, ...] = tuple(t.name for t in LOCKED_WRITE)

# 可以用 `--allow-tool` 打开的。**必须已经在 apply_mutation 里实现**，
# 否则解锁出来的是一个"看得见、一提就报未实现"的空壳。
UNLOCKABLE: dict[str, str] = {
    "delete_pvc": "删除单个 PVC。不可逆、会丢数据，所以是 severe（要手输名字）。",
}

# 任何方式都打不开的。跟 UNLOCKABLE 分开，是为了让"永久"这两个字有依据：
# 它们不是"还没实现"，而是"就算实现了也不该有这条路"。
PERMANENTLY_FORBIDDEN: dict[str, str] = {
    "delete_namespace": "删除命名空间。一次抹掉整个命名空间的全部对象，"
                        "代价无法收敛，也没有哪次值班需要它。",
    "modify_rbac": "修改 RBAC。这是**提权路径**——它能把上面所有约束一次性解开，"
                   "包括把 Agent 自己变成集群管理员。",
    "exec_in_pod": "在 Pod 内执行任意命令。等于绕过整个参数化工具层，"
                   "把集群交给模型自由发挥。",
}

_unlocked: set[str] = set()
_unlock_lock = threading.Lock()


def unlock_tool(name: str) -> None:
    """显式解锁一个禁区工具。只有 ``UNLOCKABLE`` 里的能解锁。

    解锁失败一律**抛异常**，不做"静默忽略"：启动参数写错了却照常起来，
    运维会以为自己已经打开了，实际没有——那比直接起不来更糟。
    """
    if name in PERMANENTLY_FORBIDDEN:
        raise ValueError(
            f"{name} 是**永久禁止**动作，任何参数都打不开：{PERMANENTLY_FORBIDDEN[name]}")
    if name not in UNLOCKABLE:
        rest = len(FORBIDDEN) - len(UNLOCKABLE) - len(PERMANENTLY_FORBIDDEN)
        raise ValueError(
            f"{name} 不是可解锁的动作。当前可解锁的只有：{sorted(UNLOCKABLE)}"
            f"（其余 {len(PERMANENTLY_FORBIDDEN)} 个是永久禁止，"
            f"{rest} 个还没有安全实现）")
    with _unlock_lock:
        _unlocked.add(name)


def unlocked_tools() -> dict[str, ToolSpec]:
    """已解锁的工具定义。默认返回空——**没解锁时一切照旧**。"""
    with _unlock_lock:
        return {n: s for n, s in LOCKED_SPECS.items() if n in _unlocked}


def unlocked_names() -> tuple[str, ...]:
    with _unlock_lock:
        return tuple(sorted(_unlocked))


def is_forbidden(name: str) -> bool:
    """这个工具现在是不是被禁止的？

    默认**是**（fail closed）。只有 `--allow-tool` 显式解锁过的才返回 False。
    所有拦截点都必须走这个函数，而不是直接查 ``FORBIDDEN``——否则会留下一处
    "解锁了但还是被拦"的死角，而且那种不一致极难排查。
    """
    with _unlock_lock:
        if name in _unlocked:
            return False
    return name in FORBIDDEN


def forbidden_now() -> dict[str, str]:
    """**当前仍然**被禁止的动作（已解锁的不算）。给界面和 ``omagent tools`` 用。

    为什么要跟 ``FORBIDDEN`` 分开：``FORBIDDEN`` 是"默认拒绝清单"，
    它的用途是**生成拒绝时的说明文字**，所以必须是全量、且不随运行时变化。
    而界面上要回答的是另一个问题——"现在到底什么动不了"。
    混用会出现最尴尬的显示：某个工具**同时**出现在工具清单和禁止清单里，
    运维完全没法判断它到底能不能用。
    """
    with _unlock_lock:
        opened = set(_unlocked)
    return {k: v for k, v in FORBIDDEN.items() if k not in opened}


def is_mutating(name: str) -> bool:
    """未登记的工具**默认是写操作**（fail closed）。

    宁可误拦一个只读动作，也不能把一个没声明清楚的工具当成只读放行。
    """
    spec = TOOLS.get(name)
    return True if spec is None else spec.mutating


def openai_tool_schema() -> list[dict[str, Any]]:
    """把工具清单转成 OpenAI/DeepSeek 的 function-calling 格式。

    **包含已解锁的禁区工具**——不解锁就一个都不出现，这跟以前完全一致。
    """
    def _describe(spec: ToolSpec) -> str:
        text = spec.summary
        if spec.mutating:
            text += "（写操作：调用后不会立即执行，需要运维人员确认）"
        if spec.danger == "severe":
            text += "（⚠️ 高危且不可逆：批准时必须由人手工输入目标名字）"
        return text

    specs = [*TOOLS.values(), *unlocked_tools().values()]
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": _describe(spec),
                "parameters": spec.schema,
            },
        }
        for spec in specs
    ]


def ui_tool_list() -> list[dict[str, Any]]:
    """给前端「工具能力」面板用的清单。

    **带上已解锁的禁区工具**：运维在命令行开了 `--allow-tool`，界面上却看不到，
    就会以为没生效——那正好是"配置和实际不一致"这类最难查的问题。
    同时把 danger 一起带出去，前端才能把 severe 渲染成手输确认。
    """
    return [
        {"name": s.name, "mutating": s.mutating, "summary": s.summary,
         "danger": s.danger, "unlocked": s.name in unlocked_tools()}
        for s in (*TOOLS.values(), *unlocked_tools().values())
    ]
