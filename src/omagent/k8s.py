"""Kubernetes 访问层。

两条硬性设计原则（PRD 5.2 / 5.3）：

1. **Agent 永远不拼接自由形式的 kubectl 命令字符串。** 所有操作走参数化 API。
   这是切断"模型幻觉 → 生产事故"最短路径的地方。即使规划器是 LLM，
   它能表达的也只是一组受 schema 约束的参数。
2. **所有写操作必须支持服务端 dry-run（``dry_run="All"``）。** K8s 原生能力，
   是本项目最大的免费红利：变更前先在服务端校验一遍。

连接不可用时要能优雅降级（PRD 第 10 节问题 9），而不是把值班流程堵死。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

DRY_RUN_ALL = "All"


class K8sUnavailable(RuntimeError):
    """无法连接集群。调用方应降级到只读缓存或提示用户走原流程。"""


class ActionRejected(RuntimeError):
    """API Server 拒绝了操作（含 dry-run 拒绝）。"""


@dataclass
class PodInfo:
    name: str
    phase: str
    ready: bool
    restarts: int
    namespace: str = ""
    node: str = ""
    reason: str = ""
    message: str = ""
    last_exit_code: int | None = None
    containers: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    memory_limit: str = ""
    memory_usage: str = ""
    # 终止原因（K8s 显式给出的 OOMKilled 等）。比退出码更权威：
    # 137 是 SIGKILL 的通用退出码，探针误杀、手动 kill 都会是 137。
    termination_reason: str = ""
    owner_kind: str = ""
    has_emptydir: bool = False
    node_unschedulable: bool = False

    @property
    def healthy(self) -> bool:
        return self.phase == "Running" and self.ready

    @property
    def problem(self) -> str:
        if self.phase == "Pending":
            return f"Pending: {self.reason} {self.message}".strip()
        if self.phase == "Failed":
            return f"Failed: {self.reason} {self.message}".strip()
        if not self.ready and self.phase == "Running":
            return "Running 但未 Ready（探针失败或容器启动中）"
        if self.restarts >= 5:
            return f"频繁重启（{self.restarts} 次）"
        return ""


class K8sClient:
    """参数化的 K8s 访问封装。"""

    def __init__(self, kubeconfig: str | None = None, context: str | None = None):
        self.kubeconfig = kubeconfig or os.environ.get("KUBECONFIG")
        self.context = context
        self._loaded = False
        self._load()

    def _load(self) -> None:
        try:
            if self.kubeconfig:
                config.load_kube_config(config_file=self.kubeconfig, context=self.context)
            else:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config(context=self.context)
            self._loaded = True
        except Exception as exc:  # noqa: BLE001
            raise K8sUnavailable(f"无法加载 kubeconfig: {exc}") from exc

        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.autoscaling = client.AutoscalingV1Api()
        self.policy_api = client.PolicyV1Api()
        self.networking = client.NetworkingV1Api()

    # ------------------------------------------------------------------ 探活

    def ping(self) -> tuple[bool, str]:
        try:
            ver = client.VersionApi().get_code()
            return True, f"Kubernetes {ver.git_version}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def namespaces(self) -> list[str]:
        return [ns.metadata.name for ns in self.core.list_namespace().items]

    # ------------------------------------------------------------------ 只读

    def list_pods(self, namespace: str, label_selector: str = "") -> list[PodInfo]:
        try:
            resp = self.core.list_namespaced_pod(namespace, label_selector=label_selector or None)
        except ApiException as exc:
            raise K8sUnavailable(f"list pods 失败 (ns={namespace}): {exc.reason}") from exc

        return [self._to_pod_info(p, namespace=namespace) for p in resp.items]

    @staticmethod
    def _to_pod_info(p: Any, namespace: str = "", node: str = "") -> PodInfo:
        """把 K8s Pod 对象统一转换成 PodInfo。

        单一实现，供 list_pods 与 pods_on_node 共用——两处各写一份必然漂移
        （owner_kind 的缺失就是这么来的）。
        """
        statuses = p.status.container_statuses or []
        restarts = sum(cs.restart_count for cs in statuses)
        ready = all(cs.ready for cs in statuses) if statuses else False

        last_exit = None
        term_reason = ""
        for cs in statuses:
            term = getattr(cs, "last_state", None)
            terminated = getattr(term, "terminated", None) if term is not None else None
            if terminated is not None:
                last_exit = terminated.exit_code
                term_reason = terminated.reason or ""  # "OOMKilled" / "Error" / ...

        reason = p.status.reason or ""
        message = p.status.message or ""
        for cs in statuses:
            waiting = getattr(cs.state, "waiting", None) if cs.state else None
            if waiting is not None:
                reason = waiting.reason or reason
                message = waiting.message or message

        mem_limit = ""
        containers = []
        for c in p.spec.containers:
            containers.append(c.name)
            res = c.resources
            if res and res.limits:
                mem_limit = res.limits.get("memory", "")

        owner_kind = ""
        for ref in p.metadata.owner_references or []:
            # controller=True 的那个才是真正的管理者（其余可能是 owner 链上的其他对象）
            if getattr(ref, "controller", False) or len(p.metadata.owner_references) == 1:
                owner_kind = ref.kind
                break

        has_emptydir = any(
            getattr(v, "empty_dir", None) is not None for v in (p.spec.volumes or [])
        )

        return PodInfo(
            name=p.metadata.name,
            phase=p.status.phase or "",
            ready=ready,
            restarts=restarts,
            namespace=namespace or (p.metadata.namespace or ""),
            node=node or (p.spec.node_name or ""),
            reason=reason,
            message=message,
            last_exit_code=last_exit,
            termination_reason=term_reason,
            containers=containers,
            labels=dict(p.metadata.labels or {}),
            memory_limit=mem_limit,
            owner_kind=owner_kind,
            has_emptydir=has_emptydir,
        )

    def read_workload(self, namespace: str, kind: str, name: str) -> Any:
        kind = kind.lower()
        try:
            if kind == "deployment":
                return self.apps.read_namespaced_deployment(name, namespace)
            if kind == "statefulset":
                return self.apps.read_namespaced_stateful_set(name, namespace)
            if kind == "daemonset":
                return self.apps.read_namespaced_daemon_set(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise ActionRejected(f"{kind}/{name} 在 ns={namespace} 中不存在") from exc
            raise K8sUnavailable(str(exc.reason)) from exc
        raise ActionRejected(f"不支持的 workload 类型: {kind}")

    def list_events(self, namespace: str, involved_name: str = "") -> list[dict[str, Any]]:
        try:
            resp = self.core.list_namespaced_event(namespace)
        except ApiException as exc:
            raise K8sUnavailable(str(exc.reason)) from exc
        out = []
        for e in resp.items:
            if involved_name and involved_name not in (e.involved_object.name or ""):
                continue
            out.append(
                {
                    "type": e.type,
                    "reason": e.reason,
                    "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                    "message": e.message,
                    "count": e.count,
                    "last_seen": str(e.last_timestamp or e.event_time or ""),
                }
            )
        out.sort(key=lambda x: (x["last_seen"] or ""), reverse=True)
        return out

    def pod_logs(self, namespace: str, pod: str, container: str = "", tail: int = 100) -> str:
        try:
            return self.core.read_namespaced_pod_log(
                pod,
                namespace,
                container=container or None,
                tail_lines=tail,
                timestamps=False,
            )
        except ApiException as exc:
            raise K8sUnavailable(f"读取日志失败: {exc.reason}") from exc

    def previous_pod_logs(self, namespace: str, pod: str, container: str = "", tail: int = 50) -> str:
        """读取上一个容器实例的日志——排查 CrashLoopBackOff 的关键。"""
        try:
            return self.core.read_namespaced_pod_log(
                pod, namespace, container=container or None, tail_lines=tail, previous=True
            )
        except ApiException as exc:
            return f"<无法读取上一次实例日志: {exc.reason}>"

    def list_pdb(self, namespace: str) -> list[dict[str, Any]]:
        try:
            resp = self.policy_api.list_namespaced_pod_disruption_budget(namespace)
        except ApiException:
            return []
        out = []
        for pdb in resp.items:
            spec = pdb.spec
            status = pdb.status
            out.append(
                {
                    "name": pdb.metadata.name,
                    "min_available": getattr(spec, "min_available", None),
                    "max_unavailable": getattr(spec, "max_unavailable", None),
                    "selector": (spec.selector.match_labels if spec.selector else {}) or {},
                    "current_healthy": getattr(status, "current_healthy", None),
                    "desired_healthy": getattr(status, "desired_healthy", None),
                    "disruptions_allowed": getattr(status, "disruptions_allowed", None),
                }
            )
        return out

    def list_endpoints(self, namespace: str) -> list[dict[str, Any]]:
        """列出命名空间下的 Endpoints（Service 的实际后端）。

        这是**唯一能区分"Pod 正常"与"服务可用"的信号**：
        Pod 全部 Ready 但 Endpoints 为空，说明 Service 的 selector/targetPort 有问题，
        属于 Service 配置故障而非 Pod 故障——重启 Pod 完全无效。
        """
        try:
            resp = self.core.list_namespaced_endpoints(namespace)
        except ApiException as exc:
            raise K8sUnavailable(f"list endpoints 失败 (ns={namespace}): {exc.reason}") from exc

        out: list[dict[str, Any]] = []
        for ep in resp.items:
            subsets = ep.subsets or []
            ready = sum(len(sub.addresses or []) for sub in subsets)
            not_ready = sum(len(sub.not_ready_addresses or []) for sub in subsets)
            out.append({
                "name": ep.metadata.name,
                "ready": ready,
                "not_ready": not_ready,
                "ports": sorted(
                    {pt.port for sub in subsets for pt in (sub.ports or []) if pt.port}
                ),
            })
        return out

    def services_selecting(
        self, namespace: str, labels: dict[str, str]
    ) -> list[dict[str, Any]]:
        """找出 selector 命中给定标签的 Service（即指向该工作负载的 Service）。"""
        try:
            items = self.core.list_namespaced_service(namespace).items
        except ApiException:
            return []
        out = []
        for svc in items:
            sel = dict(svc.spec.selector or {})
            if sel and all(labels.get(k) == v for k, v in sel.items()):
                out.append({
                    "name": svc.metadata.name,
                    "selector": sel,
                    # 与 list_services 保持同构，便于 targetPort 校验
                    "ports": [
                        {"port": pt.port, "target_port": str(pt.target_port)}
                        for pt in (svc.spec.ports or [])
                    ],
                })
        return out

    # ---------------------------------------------------- 配置层只读（T0）

    def workload_env(self, namespace: str, kind: str, name: str) -> list[dict[str, str]]:
        """读取工作负载 pod template 里各容器的环境变量。

        配置层诊断的基础：**应用连错了地址，答案就在 env 里**，
        但早期实现把这份信息取到了却从不读（见 docs/real-data-eval.md）。
        """
        try:
            wl = self.read_workload(namespace, kind, name)
        except Exception:  # noqa: BLE001
            return []
        tmpl = getattr(wl.spec, "template", None)
        if tmpl is None:
            return []
        out: list[dict[str, str]] = []
        for c in tmpl.spec.containers or []:
            for e in c.env or []:
                out.append({
                    "container": c.name,
                    "name": e.name,
                    "value": e.value or "",
                    "from": (
                        getattr(e.value_from, "config_map_key_ref", None) is not None
                        and "configMapKeyRef" or
                        getattr(e.value_from, "secret_key_ref", None) is not None
                        and "secretKeyRef" or ""
                    ) if e.value_from else "",
                })
        return out

    def list_services(self, namespace: str) -> list[dict[str, Any]]:
        """列出命名空间下的 Service 及其**实际暴露的端口**。

        用于交叉比对：env 里写的端口与 Service 真实端口不一致 = 配置错误。
        """
        try:
            items = self.core.list_namespaced_service(namespace).items
        except ApiException:
            return []
        out = []
        for svc in items:
            ports = []
            for p in svc.spec.ports or []:
                ports.append({"port": p.port, "target_port": str(p.target_port)})
            out.append({
                "name": svc.metadata.name,
                "selector": dict(svc.spec.selector or {}),
                "ports": ports,
            })
        return out

    def list_networkpolicies(self, namespace: str) -> list[dict[str, Any]]:
        """列出 NetworkPolicy（用于判断"依赖是好的但流量被挡"）。"""
        try:
            items = self.networking.list_namespaced_network_policy(namespace).items
        except Exception:  # noqa: BLE001
            return []
        out = []
        for np in items:
            spec = np.spec
            out.append({
                "name": np.metadata.name,
                "pod_selector": dict((spec.pod_selector.match_labels or {})
                                     if spec.pod_selector else {}),
                "policy_types": list(spec.policy_types or []),
                "ingress": bool(spec.ingress),
                "egress": bool(spec.egress),
            })
        return out

    def workload_configmaps(self, namespace: str, kind: str, name: str) -> list[str]:
        """该工作负载引用/挂载的 ConfigMap 名称（envFrom / env.valueFrom / volumes）。"""
        try:
            wl = self.read_workload(namespace, kind, name)
        except Exception:  # noqa: BLE001
            return []
        tmpl = getattr(wl.spec, "template", None)
        if tmpl is None:
            return []
        found: set[str] = set()
        for c in (getattr(tmpl.spec, "containers", None) or []):
            for ef in (getattr(c, "env_from", None) or []):
                ref = getattr(ef, "config_map_ref", None)
                if ref is not None and ref.name:
                    found.add(ref.name)
            for e in (getattr(c, "env", None) or []):
                vf = getattr(e, "value_from", None)
                ref = getattr(vf, "config_map_key_ref", None) if vf is not None else None
                if ref is not None and ref.name:
                    found.add(ref.name)
        for v in (getattr(tmpl.spec, "volumes", None) or []):
            cm = getattr(v, "config_map", None)
            if cm is not None and cm.name:
                found.add(cm.name)
        return sorted(found)

    def list_ingresses(self, namespace: str) -> list[dict[str, Any]]:
        """列出 Ingress 及其后端（service/port），用于校验后端是否真实存在。"""
        try:
            items = self.networking.list_namespaced_ingress(namespace).items
        except Exception:  # noqa: BLE001
            return []
        out = []
        for ing in items:
            backends = []
            for rule in ((ing.spec.rules or []) if ing.spec else []):
                http = getattr(rule, "http", None)
                for path in ((http.paths or []) if http else []):
                    svc = getattr(path.backend, "service", None)
                    if svc is not None:
                        port = getattr(svc.port, "number", None) or getattr(svc.port, "name", None)
                        backends.append({"service": svc.name, "port": port})
            dflt = getattr(ing.spec, "default_backend", None) if ing.spec else None
            if dflt is not None and getattr(dflt, "service", None) is not None:
                svc = dflt.service
                backends.append({
                    "service": svc.name,
                    "port": getattr(svc.port, "number", None) or getattr(svc.port, "name", None),
                })
            out.append({"name": ing.metadata.name, "backends": backends})
        return out

    def list_limitranges(self, namespace: str) -> list[dict[str, Any]]:
        """列出 LimitRange——它会给未声明资源的 Pod 强加默认值/上下限。"""
        try:
            items = self.core.list_namespaced_limit_range(namespace).items
        except Exception:  # noqa: BLE001
            return []
        out = []
        for lr in items:
            limits = []
            for item in (lr.spec.limits or []):
                limits.append({
                    "type": item.type,
                    "max": {k: str(v) for k, v in (item.max or {}).items()},
                    "min": {k: str(v) for k, v in (item.min or {}).items()},
                    "default": {k: str(v) for k, v in (item.default or {}).items()},
                })
            out.append({"name": lr.metadata.name, "limits": limits})
        return out

    def replace_configmap_data(self, namespace: str, name: str, data: dict[str, str]) -> str:
        """把 ConfigMap 的 data 整体替换为给的内容（用于回滚到历史版本）。

        这是**配置层的写动作**，风险等级 T2——必须经人工强确认。
        """
        from kubernetes import client as _client

        body = _client.V1ConfigMap(metadata=_client.V1ObjectMeta(name=name),
                                   data={k: str(v) for k, v in data.items()})
        try:
            self.core.patch_namespaced_config_map(name, namespace, body)
        except ApiException as exc:
            raise ActionRejected(f"回滚 ConfigMap 被拒绝: {exc.reason}") from exc
        return f"已将 ConfigMap/{name} 恢复为历史版本（{len(data)} 个键）"

    def read_configmap(self, namespace: str, name: str) -> dict[str, str]:
        """读取 ConfigMap 的键值（只读）。ConfigMap 本身**不含敏感凭据**，Secret 才含。"""
        try:
            cm = self.core.read_namespaced_config_map(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise ActionRejected(f"ConfigMap/{name} 在 ns={namespace} 中不存在") from exc
            raise K8sUnavailable(str(exc.reason)) from exc
        return {k: str(v)[:500] for k, v in (cm.data or {}).items()}

    def read_hpa(self, namespace: str, name: str) -> Any | None:
        try:
            return self.autoscaling.read_namespaced_horizontal_pod_autoscaler(name, namespace)
        except ApiException:
            return None

    # 会让节点上的 Pod 被驱逐/无法调度的压力条件
    PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

    def list_nodes(self) -> list[dict[str, Any]]:
        resp = self.core.list_node()
        out = []
        for n in resp.items:
            conds = {
                c.type: c.status
                for c in ((n.status.conditions or []) if n.status else [])
                if c.type in self.PRESSURE_CONDITIONS
            }
            out.append({
                "name": n.metadata.name,
                "unschedulable": bool(n.spec.unschedulable),
                "labels": dict(n.metadata.labels or {}),
                "conditions": conds,
                "pressured": [k for k, v in conds.items() if v == "True"],
                "taints": [t.key for t in (n.spec.taints or [])],
            })
        return out

    def list_resourcequotas(self, namespace: str) -> list[dict[str, Any]]:
        """列出 ResourceQuota 及用量——用于判断"Pod 创建不出来"是不是配额卡住。"""
        try:
            items = self.core.list_namespaced_resource_quota(namespace).items
        except Exception:  # noqa: BLE001
            return []
        out = []
        for q in items:
            hard = {k: str(v) for k, v in (q.status.hard or {}).items()}
            used = {k: str(v) for k, v in (q.status.used or {}).items()}
            exhausted = [k for k, v in hard.items() if used.get(k) == v]
            out.append({"name": q.metadata.name, "hard": hard, "used": used,
                        "exhausted": sorted(exhausted)})
        return out

    def pods_on_node(self, node: str) -> list[PodInfo]:
        """列出节点上的 Pod（含 DaemonSet 与系统组件）。

        刻意**不按名字前缀过滤**：drain 需要看到节点上的全部 Pod，
        尤其是 DaemonSet（kube-proxy/kindnet）——它们正是需要被"忽略"的那一类。
        早期实现既过滤了 kube-* 前缀、又没有填充 owner_kind，
        导致 ignore_daemonsets 这条安全分支永远走不到。
        """
        resp = self.core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node}")
        return [self._to_pod_info(p, node=node) for p in resp.items]

    # ------------------------------------------------------- 写操作（参数化）

    def apply_mutation(self, tool: str, params: dict[str, Any], *, dry_run: bool) -> str:
        """所有写操作的唯一入口。按工具名分发到参数化实现。

        ``dry_run=True`` 时走服务端干跑，不会产生任何副作用。
        """
        dry = DRY_RUN_ALL if dry_run else None
        ns = params.get("namespace", "")
        name = params.get("name", "")

        if tool == "rollout_restart":
            return self._rollout_restart(ns, name, params.get("kind", "deployment"), dry)
        if tool == "rollout_undo":
            return self._rollout_undo(ns, name, dry)
        if tool == "scale_workload":
            return self._scale(ns, name, params.get("kind", "deployment"), int(params["replicas"]), dry)
        if tool == "delete_pod":
            return self._delete_pod(ns, params["pod"], dry)
        if tool == "cordon_node":
            return self._cordon(params["node"], True, dry)
        if tool == "uncordon_node":
            return self._cordon(params["node"], False, dry)
        if tool == "drain_node":
            return self._drain(params["node"], ns, dry, params)
        if tool == "patch_resources":
            return self._patch_resources(ns, name, params, dry)
        if tool == "patch_hpa":
            return self._patch_hpa(ns, params["hpa"], params, dry)
        if tool == "rollback_configmap":
            data = params.get("data")
            if not isinstance(data, dict) or not data:
                raise ActionRejected("rollback_configmap 需要提供要恢复的 data")
            return self.replace_configmap_data(ns, params["name"], data)
        raise ActionRejected(f"未实现的写操作: {tool}")

    def _rollout_restart(self, ns: str, name: str, kind: str, dry: str | None) -> str:
        ts = __import__("datetime").datetime.utcnow().isoformat() + "Z"
        body = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": ts}}
                }
            }
        }
        try:
            if kind.lower() == "deployment":
                self.apps.patch_namespaced_deployment(name, ns, body, dry_run=dry)
            elif kind.lower() == "statefulset":
                self.apps.patch_namespaced_stateful_set(name, ns, body, dry_run=dry)
            else:
                raise ActionRejected(f"rollout_restart 不支持 {kind}")
        except ApiException as exc:
            raise ActionRejected(f"rollout restart 被拒绝: {exc.reason} {exc.body}") from exc
        return f"已触发 {kind}/{name} 滚动重启（restartedAt={ts}）"

    def _rollout_undo(self, ns: str, name: str, dry: str | None) -> str:
        # 通过回滚注解无法直接表达，改用 rollout undo 的等价物：
        # 读取 ReplicaSet 历史，切回上一个 revision 的 pod template。
        try:
            dep = self.apps.read_namespaced_deployment(name, ns)
            selector = dep.spec.selector.match_labels or {}
            sel = ",".join(f"{k}={v}" for k, v in selector.items())
            rss = self.apps.list_namespaced_replica_set(ns, label_selector=sel).items
            owned = [
                r
                for r in rss
                if any(
                    ref.kind == "Deployment" and ref.name == name
                    for ref in (r.metadata.owner_references or [])
                )
            ]
            live = [r for r in owned if (r.spec.replicas or 0) > 0]
            if len(owned) < 2:
                raise ActionRejected("没有可回滚的历史版本（ReplicaSet 少于 2 个）")
            live_ids = {r.metadata.name for r in live}
            candidates = [r for r in owned if r.metadata.name not in live_ids]
            if not candidates:
                raise ActionRejected("没有找到非活跃的历史 ReplicaSet，无法回滚")
            candidates.sort(key=lambda r: r.metadata.creation_timestamp, reverse=True)
            target = candidates[0]
            body = {
                "spec": {
                    "template": client.ApiClient().sanitize_for_serialization(target.spec.template)
                }
            }
            self.apps.patch_namespaced_deployment(name, ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"rollout undo 被拒绝: {exc.reason}") from exc
        return f"已回滚 {name} 到上一版本（来源 ReplicaSet: {target.metadata.name}）"

    def _scale(self, ns: str, name: str, kind: str, replicas: int, dry: str | None) -> str:
        body = {"spec": {"replicas": replicas}}
        try:
            if kind.lower() == "deployment":
                self.apps.patch_namespaced_deployment_scale(name, ns, body, dry_run=dry)
            elif kind.lower() == "statefulset":
                self.apps.patch_namespaced_stateful_set_scale(name, ns, body, dry_run=dry)
            else:
                raise ActionRejected(f"scale 不支持 {kind}")
        except ApiException as exc:
            raise ActionRejected(f"scale 被拒绝: {exc.reason}") from exc
        return f"已将 {kind}/{name} 副本数调整为 {replicas}"

    def _delete_pod(self, ns: str, pod: str, dry: str | None) -> str:
        try:
            self.core.delete_namespaced_pod(pod, ns, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"删除 Pod 被拒绝: {exc.reason}") from exc
        return f"已删除 Pod {pod}（控制器将自动重建）"

    def _cordon(self, node: str, unschedulable: bool, dry: str | None) -> str:
        body = {"spec": {"unschedulable": unschedulable}}
        try:
            self.core.patch_node(node, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"cordon 被拒绝: {exc.reason}") from exc
        verb = "cordon" if unschedulable else "uncordon"
        return f"已对节点 {node} 执行 {verb}"

    def _drain(
        self, node: str, ns: str, dry: str | None, params: dict[str, Any] | None = None
    ) -> str:
        """节点排空。

        完整实现 kubectl drain 的核心语义：

        - 先 cordon，避免新 Pod 继续调度上来
        - **DaemonSet 管理的 Pod 默认跳过**（驱逐了也会立刻重建，是经典陷阱）；
          只有显式 ``ignore_daemonsets=True`` 才放行——但即便如此也**不会**驱逐它们，
          kubectl 的语义同样是"忽略"，而非"删除"
        - **使用 emptyDir 的 Pod 默认跳过**（数据会丢失）；需显式
          ``delete_emptydir_data=True`` 才允许
        - 驱逐失败的 Pod（多为 PDB 拒绝）被跳过并记录原因，不会中断整个排空
        """
        params = params or {}
        ignore_daemonsets = bool(params.get("ignore_daemonsets", False))
        delete_emptydir = bool(params.get("delete_emptydir_data", False))

        self._cordon(node, True, dry)
        pods = self.pods_on_node(node)

        evicted: list[str] = []
        skipped_ds: list[str] = []
        skipped_emptydir: list[str] = []
        blocked: list[str] = []

        for p in pods:
            if p.owner_kind == "DaemonSet":
                # kubectl drain 的语义是"忽略"，不是"删除"——重建没有意义
                skipped_ds.append(p.name)
                continue
            if p.has_emptydir and not delete_emptydir:
                skipped_emptydir.append(p.name)
                continue
            try:
                body = client.V1Eviction(
                    metadata=client.V1ObjectMeta(name=p.name, namespace=p.namespace)
                )
                # 注意：eviction 子资源挂在 CoreV1Api 上，不在 PolicyV1Api。
                # 这个错误在"简化版 drain 从未被真正调用"时不会暴露，
                # 属于典型的"没跑过的代码等于没有的代码"。
                self.core.create_namespaced_pod_eviction(
                    p.name, p.namespace, body, dry_run=dry
                )
                evicted.append(p.name)
            except ApiException as exc:
                # PDB 拒绝驱逐是**预期行为**，不是错误
                blocked.append(f"{p.name}({exc.reason or 'eviction denied'})")

        lines = [f"节点 {node} 已 cordon（不再接受新 Pod）"]
        lines.append(f"成功驱逐 {len(evicted)} 个 Pod")
        if skipped_ds:
            lines.append(
                f"跳过 {len(skipped_ds)} 个 DaemonSet Pod"
                + ("（已显式忽略 DaemonSet）" if ignore_daemonsets else "（未开启 ignore_daemonsets）")
            )
        if skipped_emptydir:
            lines.append(
                f"跳过 {len(skipped_emptydir)} 个使用 emptyDir 的 Pod（未开启 delete_emptydir_data，数据会丢失）"
            )
        if blocked:
            lines.append(f"被 PDB 拒绝 {len(blocked)} 个：{', '.join(blocked[:5])}")
        if not evicted and (skipped_ds or blocked or skipped_emptydir):
            lines.append("⚠️ 没有任何 Pod 被成功驱逐——节点可能仍未真正排空")
        return "；".join(lines)

    def _patch_resources(self, ns: str, name: str, params: dict[str, Any], dry: str | None) -> str:
        container = params["container"]
        res: dict[str, Any] = {}
        if params.get("cpu_request") or params.get("memory_request"):
            res["requests"] = {
                k: v
                for k, v in (
                    ("cpu", params.get("cpu_request")),
                    ("memory", params.get("memory_request")),
                )
                if v
            }
        if params.get("cpu_limit") or params.get("memory_limit"):
            res["limits"] = {
                k: v
                for k, v in (
                    ("cpu", params.get("cpu_limit")),
                    ("memory", params.get("memory_limit")),
                )
                if v
            }
        if not res:
            raise ActionRejected("patch_resources 至少需要一项 resources 参数")
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": container, "resources": res},
                        ]
                    }
                }
            }
        }
        try:
            self.apps.patch_namespaced_deployment(name, ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"patch resources 被拒绝: {exc.reason} {exc.body}") from exc
        return f"已更新 {name}/{container} 的资源规格: {res}"

    def _patch_hpa(self, ns: str, name: str, params: dict[str, Any], dry: str | None) -> str:
        body: dict[str, Any] = {"spec": {}}
        if params.get("min_replicas") is not None:
            body["spec"]["minReplicas"] = int(params["min_replicas"])
        if params.get("max_replicas") is not None:
            body["spec"]["maxReplicas"] = int(params["max_replicas"])
        if not body["spec"]:
            raise ActionRejected("patch_hpa 至少需要 min_replicas 或 max_replicas")
        try:
            self.autoscaling.patch_namespaced_horizontal_pod_autoscaler(name, ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"patch HPA 被拒绝: {exc.reason}") from exc
        return f"已更新 HPA/{name}: {body['spec']}"

    # ------------------------------------------------- T0 只读诊断（真正可执行）
    #
    # 此前 T0 工具只是"能力声明"（用来告诉模型可以查什么），并不会真的被执行。
    # 这里补齐执行能力，使 Agent 能做真正意义上的"先取证、再下结论"循环。

    def read_only_query(self, tool: str, params: dict[str, Any]) -> str:
        """执行一次只读诊断，返回人类可读的文本结果。

        只读动作自动放行（T0），因此不经过审批门禁——但仍必须是**参数化**的，
        不接受自由形式的 kubectl 命令。
        """
        ns = params.get("namespace", "")
        handler = {
            "get_pods": self._q_pods,
            "get_events": self._q_events,
            "get_logs": self._q_logs,
            "get_pdb": self._q_pdb,
            "get_workload": self._q_workload,
            "get_nodes": self._q_nodes,
            "get_endpoints": self._q_endpoints,
            "get_services": self._q_services,
            "get_configmap": self._q_configmap,
        }.get(tool)
        if handler is None:
            raise ActionRejected(f"{tool!r} 不是已实现的只读诊断动作")
        return handler(ns, params)

    def _q_pods(self, ns: str, params: dict[str, Any]) -> str:
        selector = params.get("label_selector", "")
        pods = self.list_pods(ns, label_selector=selector)
        if not pods:
            return f"命名空间 {ns} 下没有匹配的 Pod"
        lines = []
        for p in pods:
            state = "Ready" if p.healthy else (p.problem or p.phase)
            lines.append(
                f"{p.name}  phase={p.phase} ready={p.ready} "
                f"restarts={p.restarts} exit={p.last_exit_code} limit={p.memory_limit or '-'} "
                f"| {state}"
            )
        return "\n".join(lines)

    def _q_events(self, ns: str, params: dict[str, Any]) -> str:
        events = self.list_events(ns, involved_name=params.get("name", ""))
        if not events:
            return f"命名空间 {ns} 下没有相关事件"
        return "\n".join(
            f"[{e['type']}] {e['reason']} {e['object']}: {e['message'][:200]}"
            for e in events[:15]
        )

    def _q_logs(self, ns: str, params: dict[str, Any]) -> str:
        pod = params["pod"]
        container = params.get("container", "")
        tail = int(params.get("tail_lines", 40))
        if params.get("previous"):
            return self.previous_pod_logs(ns, pod, container=container, tail=tail)
        return self.pod_logs(ns, pod, container=container, tail=tail)

    def _q_pdb(self, ns: str, params: dict[str, Any]) -> str:
        pdbs = self.list_pdb(ns)
        if not pdbs:
            return f"命名空间 {ns} 下没有 PodDisruptionBudget"
        return "\n".join(
            f"{p['name']}: minAvailable={p.get('min_available')} "
            f"maxUnavailable={p.get('max_unavailable')} "
            f"允许中断={p.get('disruptions_allowed')} selector={p.get('selector')}"
            for p in pdbs
        )

    def _q_workload(self, ns: str, params: dict[str, Any]) -> str:
        kind = params.get("kind", "deployment")
        name = params.get("name", "")
        wl = self.read_workload(ns, kind, name)
        out = [
            f"{kind}/{name} replicas={wl.spec.replicas}",
            f"labels={dict(wl.metadata.labels or {})}",
        ]
        tmpl = getattr(wl.spec, "template", None)
        if tmpl is not None:
            for c in tmpl.spec.containers or []:
                res = c.resources
                out.append(
                    f"container {c.name}: image={c.image} "
                    f"limits={dict(res.limits or {}) if res and res.limits else {}} "
                    f"requests={dict(res.requests or {}) if res and res.requests else {}}"
                )
            out.append(f"nodeSelector={dict(tmpl.spec.node_selector or {})}")
        return "\n".join(out)

    def _q_services(self, ns: str, params: dict[str, Any]) -> str:
        """列出 Service 及其实端口——配置层交叉比对要用。

        注意：这个工具曾在 TOOLS 与策略里登记、却**漏接了分派器**，
        导致 LLM 一旦调用就拿到 ActionRejected、只能回答"证据不足"。
        真正的修复不是补这一行，而是补那组"替身保真度"测试。
        """
        svcs = self.list_services(ns)
        if not svcs:
            return f"命名空间 {ns} 下没有 Service"
        lines = []
        for s_ in svcs:
            ports = ", ".join(f"{p['port']}->{p['target_port']}" for p in s_["ports"])
            lines.append(f"{s_['name']}  ports={ports or '-'}  selector={s_['selector']}")
        return "\n".join(lines)

    def _q_configmap(self, ns: str, params: dict[str, Any]) -> str:
        """读取 ConfigMap 内容（只读；永不接触 Secret）。"""
        name = params.get("name", "")
        if not name:
            raise ActionRejected("get_configmap 需要 name")
        data = self.read_configmap(ns, name)
        if not data:
            return f"ConfigMap/{name} 为空"
        return "\n".join(f"{k} = {v}" for k, v in data.items())

    def _q_nodes(self, ns: str, params: dict[str, Any]) -> str:
        nodes = self.list_nodes()
        return "\n".join(
            f"{n['name']} unschedulable={n['unschedulable']}" for n in nodes
        )

    def _q_endpoints(self, ns: str, params: dict[str, Any]) -> str:
        """查 Endpoints —— 判断"Service 有没有可用后端"的关键证据。"""
        name = params.get("name", "")
        try:
            if name:
                ep = self.core.read_namespaced_endpoints(name, ns)
                items = [ep]
            else:
                items = self.core.list_namespaced_endpoints(ns).items
        except ApiException as exc:
            return f"查询 Endpoints 失败: {exc.reason}"
        out = []
        for ep in items:
            subsets = ep.subsets or []
            ready = sum(len(s.addresses or []) for s in subsets)
            not_ready = sum(len(s.not_ready_addresses or []) for s in subsets)
            out.append(f"{ep.metadata.name}: ready={ready} notReady={not_ready}")
        return "\n".join(out) if out else f"命名空间 {ns} 下没有 Endpoints"
