"""测试替身：不碰真集群、不调真模型。

这样测试跑起来是毫秒级的，而且可以在没有网络、没有 kind 集群的机器上跑。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from omagent.llm import LLMReply, ToolCall


class FakeK8s:
    """记录所有调用，并按脚本返回结果的假客户端。"""

    def __init__(self) -> None:
        self.readonly_calls: list[tuple[str, dict[str, Any]]] = []
        self.mutation_calls: list[tuple[str, dict[str, Any], bool]] = []
        self.readonly_result = "fake-readonly-output"
        self.dry_run_ok = True
        # 干跑结果和真实执行结果分开，测试才能只改其中一个
        self.dry_run_result = "fake-dry-run-output"
        self.mutation_result = "fake-mutation-output"

    def ping(self) -> tuple[bool, str]:
        return True, "fake-k8s v1.0"

    def read_only_query(self, tool: str, params: dict[str, Any]) -> str:
        self.readonly_calls.append((tool, dict(params)))
        return f"{self.readonly_result}:{tool}"

    def apply_mutation(self, tool: str, params: dict[str, Any], *, dry_run: bool) -> str:
        self.mutation_calls.append((tool, dict(params), dry_run))
        if dry_run:
            if not self.dry_run_ok:
                from omagent.k8s import ActionRejected

                raise ActionRejected("fake dry-run 拒绝")
            return f"{self.dry_run_result}:{tool}"
        return f"{self.mutation_result}:{tool}"

    # impact.py 会调用它；失败会被 agent 捕获并记进 notes，这里给个够用的假对象
    def read_workload(self, namespace: str, kind: str, name: str) -> Any:
        raise RuntimeError("fake: 影响面分析不可用")


class ScriptedLLM:
    """按剧本返回回复的假模型。剧本用完后一直返回最后一条。"""

    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    @property
    def available(self) -> bool:
        return True

    def chat(self, messages, tools=None, *, temperature=0.2) -> LLMReply:
        self.calls.append([dict(m) for m in messages])
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


def reply_text(text: str) -> LLMReply:
    return LLMReply(content=text, finish_reason="stop")


def reply_tools(*calls: tuple[str, dict[str, Any]]) -> LLMReply:
    return LLMReply(
        content="",
        tool_calls=[ToolCall(id=f"call_{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)],
        finish_reason="tool_calls",
    )


# ---------------------------------------------------------------------------
# 假 K8s 对象工厂 —— 给 console.py 的测试用
# ---------------------------------------------------------------------------


def pod(name: str, *, ready: bool = True, phase: str = "Running", restarts: int = 0,
        labels: dict[str, str] | None = None, reason: str = "", node: str = "n1") -> Any:
    return SimpleNamespace(
        name=name, phase=phase, ready=ready, restarts=restarts,
        labels=labels or {"app": "x"}, namespace="demo", node=node,
        reason=reason, message="", last_exit_code=None, termination_reason="",
        memory_limit="128Mi", healthy=(phase == "Running" and ready),
        problem="" if (phase == "Running" and ready) else (reason or f"{phase}"),
    )


def container(name: str = "app", image: str = "img:1", ports: tuple[int, ...] = (8080,),
              configmaps: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(
        name=name, image=image,
        ports=[SimpleNamespace(container_port=p) for p in ports],
        env_from=[SimpleNamespace(config_map_ref=SimpleNamespace(name=c)) for c in configmaps],
    )


def workload(name: str, kind: str = "deployment", *, replicas: int | None = 2,
             ready: int | None = None, labels: dict[str, str] | None = None,
             containers: list[Any] | None = None, node_pool: str = "",
             has_pvc: bool = False) -> Any:
    labels = labels or {"app": name}
    volumes = []
    if has_pvc:
        volumes = [SimpleNamespace(persistent_volume_claim=SimpleNamespace(claim_name="data"))]
    spec = SimpleNamespace(
        replicas=replicas,
        selector=SimpleNamespace(match_labels=labels),
        # impact.py 会读这两个字段，缺了会抛 AttributeError 被吞掉，
        # 导致影响面变成一片空白——假对象必须把真实对象的结构补全。
        volume_claim_templates=["data"] if (kind == "statefulset" and has_pvc) else None,
        template=SimpleNamespace(
            metadata=SimpleNamespace(labels=labels),
            spec=SimpleNamespace(
                containers=containers or [container()],
                volumes=volumes,
                node_selector={"node-pool": node_pool} if node_pool else {},
            ),
        ),
    )
    if kind == "daemonset":
        # DaemonSet 没有 spec.replicas，期望值在 status 里
        spec.replicas = None
        status = SimpleNamespace(desired_number_scheduled=replicas or 0,
                                 number_ready=replicas if ready is None else ready)
    else:
        r = replicas or 0
        status = SimpleNamespace(ready_replicas=r if ready is None else ready,
                                 available_replicas=r if ready is None else ready)
    obj = SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"app": name}),
        spec=spec, status=status,
    )
    # console 的分派靠 kind 区分 Deployment/StatefulSet/DaemonSet，
    # 但真实的 K8s 对象上是靠类型而不是字段。测试里挂一个属性最省事。
    obj.kind = kind
    return obj


def hpa(name: str, target: str, conditions: list[tuple[str, str, str]]) -> Any:
    """conditions: [(type, status, reason), ...]"""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(scale_target_ref=SimpleNamespace(name=target, kind="Deployment")),
        status=SimpleNamespace(conditions=[
            SimpleNamespace(type=t, status=s, reason=r, message="") for t, s, r in conditions
        ]),
    )


class FakeConsoleK8s:
    """只实现 console.py 用到的那几个查询。"""

    def __init__(self, *, pods=None, workloads=None, services=None, endpoints=None,
                 hpas=None, events=None, nodes=None) -> None:
        self._pods = pods or []
        self._workloads = workloads or []
        self._services = services or []
        self._endpoints = endpoints or {}
        self._hpas = hpas or []
        self._events = events or []
        self._nodes = nodes or []

        def items_for(kind: str):
            def _list(_ns):
                return SimpleNamespace(
                    items=[w for w in self._workloads if getattr(w, "kind", "") == kind]
                )
            return _list

        self.apps = SimpleNamespace(
            list_namespaced_deployment=items_for("deployment"),
            list_namespaced_stateful_set=items_for("statefulset"),
            list_namespaced_daemon_set=items_for("daemonset"),
        )
        self.autoscaling_v2 = SimpleNamespace(
            list_namespaced_horizontal_pod_autoscaler=lambda _ns: SimpleNamespace(items=self._hpas)
        )

    def list_pods(self, namespace, label_selector=""):
        return self._pods

    def list_events(self, namespace): return self._events
    def list_nodes(self): return self._nodes
    def node_usage(self): return {}
    def pods_per_node(self): return {}
    def list_services(self, namespace): return self._services
    def list_endpoints(self, namespace):
        return [{"name": k, **v} for k, v in self._endpoints.items()]
