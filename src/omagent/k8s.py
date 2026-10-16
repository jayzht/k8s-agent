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

import ast
import os
from dataclasses import dataclass, field
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

DRY_RUN_ALL = "All"

# Deployment 和它派生的每个 ReplicaSet 都带这个注解，值是发布版本号。
#
# ⚠️ 不要用 ``rollout.kubernetes.io/revision``：那不是 K8s 写过的注解键，
# 照它去找版本会**永远找不到任何历史**（而且不报错，只是"没有可回滚版本"）。
# 这个坑很隐蔽，所以只认这一个规范键。
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


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


# ---------------------------------------------------------------------------
# K8s 数量串解析
#
# K8s 返回的资源量是带后缀的字符串：``213603259n``（纳核）、``941856Ki``、
# ``1500m``、``2Gi``……不解析就只能原样显示，人和模型都看不出"到底用了多少"。
# ---------------------------------------------------------------------------

_QTY_SUFFIX = {
    "": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15,
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5,
}


def coerce_text(data: Any) -> str:
    """把日志端点的返回体规整成真正的 UTF-8 文本。

    正常情况下我们拿到的是 bytes（见 ``_read_log``）。保底分支处理的是
    kubernetes 客户端在某些版本上的行为：它把 text/plain 的响应体当成
    ``response_type="str"`` 反序列化，于是 ``str(b'...')`` 产生了「字节串 repr」
    字符串——真实换行退化成字面量 ``\\n``，非 ASCII 退化成 ``\\xNN`` 转义。
    这种字符串会让 Agent 读到乱码、让注入检测器看不到中文，所以必须还原。

    只在能精确往返（``repr(还原结果) == 原字符串``）时才替换，避免把一条
    恰好以 ``b'`` 开头的正常日志误判成 repr。
    """
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    if not isinstance(data, str):
        return str(data)
    if data[:1] != "b" or data[1:2] not in ("'", '"') or data[-1:] != data[1:2]:
        return data
    if "\\" not in data:
        return data
    try:
        recovered = ast.literal_eval(data)
    except (ValueError, SyntaxError):
        return data
    if isinstance(recovered, bytes) and repr(recovered) == data:
        return recovered.decode("utf-8", errors="replace")
    return data


def parse_quantity(text: str | int | float | None) -> float:
    """把 ``1500m`` / ``2Gi`` / ``941856Ki`` 这类串解析成浮点数。

    解析不了就返回 0——指标缺失不该让整个面板塌掉。
    """
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text).strip()
    if not raw:
        return 0.0
    # 后缀从长到短匹配，先试 Ki/Mi/Gi 再试 k/M/G，否则 "Gi" 会被 "G" 吃掉
    for suffix in sorted(_QTY_SUFFIX, key=len, reverse=True):
        if suffix and raw.endswith(suffix):
            try:
                return float(raw[: -len(suffix)]) * _QTY_SUFFIX[suffix]
            except ValueError:
                return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def human_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"


# ---------------------------------------------------------------------------
# Chaos Mesh 实验对象
# ---------------------------------------------------------------------------
#
# 为什么**必须直接读对象**，而不是只看事件：
#
# 实测（2026-10-16）证明事件这条通道**会漏**，而且漏得有规律：
#   · `get_events` 只回最近 15 条（按 last_seen 排）。demo 命名空间里
#     busybox `nc` 单连接模型导致 **42 条 Unhealthy 探针失败事件常年刷新**，
#     单这一个 reason 就塞满窗口；
#   · 更糟的是故障自己会制造更多更新的噪音：StressChaos 把 api-gateway 的
#     CPU 打到限额 → HPA 从 6 扩到 8 → 又产生约 30 条 Pod 调度/创建事件。
#     实测结果：`StressChaos/om-stress-cpu` 的 8 条事件**排在第 33~40 位**，
#     模型根本看不到。
#   · 事件还有 TTL，并且**可以被清理掉**——"静默注入"就是这么做的
#     （boutique 那次：实验对象还在，命名空间事件被清空，
#      于是模型读到"没有相关事件"，进而把下游延迟误判成调用方 CPU 限流）。
#
# 而实验对象本身是**持久的、数量极少的、不可能被噪音挤掉的**。两者是
# "可能看不见"和"一定看得见"的区别，所以这里读对象，事件只当补充。
#
# 只读，不涉及任何写路径；CRD 没装时返回空列表，绝不报错（没有 Chaos Mesh
# 的集群里这个功能应当静默消失，而不是让排障工具挂掉）。
CHAOS_GROUP = "chaos-mesh.org"
CHAOS_VERSION = "v1alpha1"
CHAOS_KINDS: tuple[str, ...] = (
    "NetworkChaos", "StressChaos", "PodChaos", "DNSChaos", "HTTPChaos",
    "TimeChaos", "IOChaos", "JVMChaos", "KernelChaos", "BlockChaos",
    "PhysicalMachineChaos",
)


def _chaos_detail(kind: str, spec: dict[str, Any]) -> str:
    """把不同 kind 的 Chaos 压成一句"它到底在干什么"。

    为什么要分 kind：``spec.action`` 只有 NetworkChaos / PodChaos / DNSChaos
    这些才有；StressChaos 用的是 ``stressors``，IOChaos 用的是 ``methods``。
    只读 ``action`` 的话，StressChaos 会渲染成一行**没有动作**的空壳——
    而"到底是 CPU 打满还是内存打满"恰恰是这个场景最关键的一个词。
    """
    action = str(spec.get("action") or "")
    bits: list[str] = []
    if kind == "StressChaos":
        st = spec.get("stressors") or {}
        cpu = st.get("cpu") or {}
        mem = st.get("memory") or {}
        if cpu:
            bits.append(f"CPU 打满（workers={cpu.get('workers', '?')} "
                        f"load={cpu.get('load', '?')}）")
        if mem:
            bits.append(f"内存压力（workers={mem.get('workers', '?')} "
                        f"size={mem.get('size', '?')}）")
    elif kind == "NetworkChaos":
        if action:
            bits.append(action)
        delay = spec.get("delay") or {}
        loss = spec.get("loss") or {}
        if delay:
            bits.append(f"延迟={delay.get('latency', '?')}"
                        f"（jitter={delay.get('jitter', '0ms')}）")
        if loss:
            bits.append(f"丢包={loss.get('loss', '?')}")
        if action == "partition":
            bits.append("网络被切断")
    elif kind in ("PodChaos", "DNSChaos", "HTTPChaos", "TimeChaos") and action:
        bits.append(action)
    elif action:
        bits.append(action)
    return "  ".join(bits)


def _chaos_selector_text(sel: Any) -> str:
    """把 Chaos 的 selector/target 压成一行人话（标签 + 命名空间）。"""
    if not isinstance(sel, dict):
        return ""
    parts: list[str] = []
    labels = sel.get("labelSelectors") or {}
    if isinstance(labels, dict) and labels:
        parts.append(" ".join(f"{k}={v}" for k, v in sorted(labels.items())))
    ns = sel.get("namespaces") or []
    if isinstance(ns, (list, tuple)) and ns:
        parts.append("ns=" + ",".join(str(x) for x in ns))
    return " ".join(parts)


def _dedupe_by_reason(
    events: list[dict[str, Any]], *, limit: int = 15, per_reason: int = 3,
) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """按 ``last_seen`` 倒序选事件，但同一个 reason 最多选 ``per_reason`` 条。

    为什么需要：真实集群里"同一件事重复发生"的事件（探针失败、Pod 反复调度）
    会按新近度把真正稀有的那条（Chaos 注入、HPA 触顶）挤到窗口之外。
    限制单一 reason 的占比，是让窗口回到"看有多少**种**异常"而不是
    "看同一件事刷了多少遍"。

    返回 ``(选中的事件, [(被折叠的 reason, 条数), ...])``——被折叠的必须报出来，
    否则就等于把截断藏起来，那正是这个函数要修的那个毛病。
    """
    kept: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    folded: dict[str, int] = {}
    for e in events:
        reason = e.get("reason") or "-"
        if seen.get(reason, 0) < per_reason:
            seen[reason] = seen.get(reason, 0) + 1
            if len(kept) < limit:
                kept.append(e)
        else:
            folded[reason] = folded.get(reason, 0) + 1
    return kept, sorted(folded.items(), key=lambda kv: -kv[1])


class K8sClient:
    """参数化的 K8s 访问封装。"""

    def __init__(self, kubeconfig: str | None = None, context: str | None = None):
        self.kubeconfig = kubeconfig or os.environ.get("KUBECONFIG")
        self.context = context
        self._loaded = False
        self._load()

    def _load(self) -> None:
        """按"显式指定 → in-cluster → 默认 ~/.kube/config"的顺序加载凭据。

        为什么 KUBECONFIG 指向的文件不存在时**不直接报错**：容器镜像里
        `KUBECONFIG` 有个默认值 `/kubeconfig/config`（对应文档里让你挂载的路径）。
        但同一个镜像也可能被部署成 Pod、走 ServiceAccount。那种场景下这个路径
        不存在，硬报错就会让"集群内跑"这个用法彻底不可用。

        踩过的坑：文档里那条 `docker run` 挂了 kubeconfig 却没设 `KUBECONFIG`，
        于是走了 in-cluster 分支并报 "Service host/port is not set"——
        挂载是对的，缺的只是一句话。现在两头都兜住了。
        """
        try:
            if self.kubeconfig and os.path.exists(self.kubeconfig):
                config.load_kube_config(config_file=self.kubeconfig, context=self.context)
            elif self.kubeconfig:
                # 指定了但文件不在：优先当"集群内部署"处理，其次才是默认路径
                if not self._load_incluster_or_default():
                    raise K8sUnavailable(
                        f"KUBECONFIG 指向 {self.kubeconfig}，但文件不存在；"
                        "也不在集群内（没有 service account）。"
                        "要么把 kubeconfig 挂到那个路径，要么改成正确的路径。"
                    )
            else:
                if not self._load_incluster_or_default():
                    raise K8sUnavailable(
                        "找不到任何集群凭据：没设 KUBECONFIG、不在集群内、"
                        "~/.kube/config 也不存在。"
                    )
            self._loaded = True
        except K8sUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise K8sUnavailable(f"无法加载 kubeconfig: {exc}") from exc

        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.autoscaling = client.AutoscalingV1Api()
        # HPA 的 conditions 只有 **v2** 才暴露。用 v1 读到的
        # V1HorizontalPodAutoscalerStatus 上根本没有 conditions 属性——
        # 读取时会抛 AttributeError，把"HPA 失效了"这个信号整个吞掉。
        self.autoscaling_v2 = client.AutoscalingV2Api()
        # metrics.k8s.io 是聚合 API，走 CustomObjects 而不是某个类型化客户端
        self.custom = client.CustomObjectsApi()
        self.policy_api = client.PolicyV1Api()
        self.networking = client.NetworkingV1Api()
        # 自检用：问 API Server "我能不能干这个"，比自己试一次干净（无副作用）
        self.authz = client.AuthorizationV1Api()

    # ---------------------------------------------------------- 配置类写操作
    #
    # 这四个是"处置能力的天花板"——在此之前，评测里有 4 个场景
    # （selector / port / probe / pending）的正解都是"明说我没有工具"。
    # 它们不是危险的变更，是**最普通的运维修复**，缺了它们等于放弃一大块工作。

    def _patch_service_selector(self, ns: str, service: str,
                                selector: dict[str, str], dry: str | None) -> str:
        """改 Service 的 selector。选错会让流量**全部送不到**，所以是 caution。"""
        if not selector:
            raise ActionRejected("selector 不能为空——那会让 Service 失去所有后端")
        self.core.patch_namespaced_service(
            service, ns, {"spec": {"selector": dict(selector)}}, dry_run=dry)
        return f"已把 Service/{service} 的 selector 改成 {dict(selector)}"

    def _patch_service_port(self, ns: str, service: str,
                            params: dict[str, Any], dry: str | None) -> str:
        """改某个端口的 targetPort。按端口名或 port 号定位。"""
        cur = self.core.read_namespaced_service(service, ns)
        want_name = str(params.get("port_name") or "")
        want_port = params.get("port")
        target = params["target_port"]
        body, hit = [], False
        for i, p in enumerate(cur.spec.ports or []):
            if want_name and p.name == want_name:
                pass
            elif want_port is not None and int(p.port) == int(want_port):
                pass
            else:
                continue
            hit = True
            body.append({"op": "replace", "path": f"/spec/ports/{i}/targetPort",
                         "value": target})
        if not hit:
            raise ActionRejected(
                f"Service/{service} 里找不到端口 {want_name or want_port}"
                f"（现有：{[p.name or p.port for p in cur.spec.ports or []]}）")
        self.core.patch_namespaced_service(service, ns, body, dry_run=dry)
        return f"已把 Service/{service} 的 targetPort 改成 {target}"

    def _patch_probe(self, ns: str, name: str, params: dict[str, Any],
                     dry: str | None) -> str:
        """改探针端口。probe/handler 都能改。"""
        kind = params.get("kind", "deployment")
        container = str(params.get("container") or "app")
        which = str(params.get("probe") or "liveness")
        if which not in ("liveness", "readiness", "startup"):
            raise ActionRejected(f"probe 只能是 liveness/readiness/startup，收到 {which!r}")
        field = {"liveness": "livenessProbe", "readiness": "readinessProbe",
                 "startup": "startupProbe"}[which]
        spec = self._container_index(ns, kind, name, container)
        path = (f"/spec/template/spec/containers/{spec['index']}"
                f"/{field}/httpGet/port")
        self._patch_workload(ns, kind, name,
                             [{"op": "replace", "path": path,
                               "value": int(params["port"])}], dry)
        return f"已把 {kind}/{name} 的 {container} {which} 探针端口改成 {params['port']}"

    def _patch_node_selector(self, ns: str, name: str, kind: str,
                             params: dict[str, Any], dry: str | None) -> str:
        """设置或**删除** nodeSelector 的键。

        ⚠️ 删除必须用 JSON patch 的 ``remove``，**merge patch 删不掉 map 里已有的键**。
        这个坑我实测过：回滚一个新加 nodeSelector 的 Deployment，merge patch
        之后那个键**还在**，而人以为回滚成功了。
        """
        cur = self.read_workload(ns, kind, name)
        existing = dict(getattr(cur.spec.template.spec, "node_selector", None) or {})
        add = dict(params.get("selector") or {})
        drop = list(params.get("remove") or [])
        unknown = [k for k in drop if k not in existing]
        if unknown:
            raise ActionRejected(f"nodeSelector 里没有这些键，删不掉：{unknown}")

        if not add and not drop:
            raise ActionRejected("既没有要新增的 selector，也没有要删的键")
        want = dict(existing)
        for k in drop:
            want.pop(k, None)
        want.update(add)

        # ⚠️ 这里用 **replace 整个对象**，不是 patch。
        #
        # 试过两条 patch 路，都不行：
        #   merge patch —— 语法上就删不掉 map 里已有的键（这是它的定义）
        #   JSON patch  —— 要显式指定 Content-Type，客户端那条路径实测被 API Server
        #                  以 422 拒了（列表 body 被当成 merge patch 发出去了）
        # 读出来改完再 replace 是唯一不会踩语义坑的做法：**我们本来就已经把对象
        # 读出来了**（要先知道有哪些键才能删），所以代价只是多传一次完整对象。
        cur.spec.template.spec.node_selector = want or None
        try:
            fn = {"deployment": self.apps.replace_namespaced_deployment,
                  "statefulset": self.apps.replace_namespaced_stateful_set}[kind]
        except KeyError:
            raise ActionRejected(f"不支持对 {kind!r} 做这类修改") from None
        try:
            fn(name, ns, cur, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"修改被拒绝: {exc.reason} {exc.body}") from exc
        parts = []
        if drop:
            parts.append(f"删掉 {drop}")
        if add:
            parts.append(f"设为 {add}")
        return f"已把 {kind}/{name} 的 nodeSelector " + "，".join(parts)

    def _patch_workload(self, ns: str, kind: str, name: str,
                        body: Any, dry: str | None) -> None:
        """按 kind 分发 patch。**列表 body 走 JSON patch，字典走 merge patch。**

        这个区分是必须的：merge patch **删不掉 map 里已有的键**。
        所以"删掉一个 nodeSelector"只能用 JSON patch 的 ``remove``。
        """
        fns = {
            "deployment": self.apps.patch_namespaced_deployment,
            "statefulset": self.apps.patch_namespaced_stateful_set,
        }
        fn = fns.get(kind)
        if fn is None:
            raise ActionRejected(f"不支持对 {kind!r} 做这类 patch")
        kwargs: dict[str, Any] = {"dry_run": dry}
        if isinstance(body, list):
            kwargs["_content_type"] = "application/json-patch+json"
        try:
            fn(name, ns, body, **kwargs)
        except ApiException as exc:
            raise ActionRejected(f"patch 被拒绝: {exc.reason} {exc.body}") from exc

    def _container_index(self, ns: str, kind: str, name: str, container: str) -> dict[str, Any]:
        cur = self.read_workload(ns, kind, name)
        for i, c in enumerate(cur.spec.template.spec.containers or []):
            if c.name == container:
                return {"index": i, "container": c}
        raise ActionRejected(
            f"{kind}/{name} 里没有名为 {container!r} 的容器"
            f"（实际：[{[c.name for c in cur.spec.template.spec.containers or []]}]）")

    # ------------------------------------------------------------------ 探活

    @staticmethod
    def _load_incluster_or_default() -> bool:
        """先试集群内凭据，再试 ~/.kube/config。成功返回 True。"""
        try:
            config.load_incluster_config()
            return True
        except Exception:  # noqa: BLE001
            pass
        try:
            config.load_kube_config()
            return True
        except Exception:  # noqa: BLE001
            return False

    def ping(self) -> tuple[bool, str]:
        try:
            ver = client.VersionApi().get_code()
            return True, f"Kubernetes {ver.git_version}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def metrics_available(self) -> bool:
        """装没装 metrics-server。

        没装的话 get_metrics 会返回空，而"内存够不够"恰恰是最常被问的问题——
        自检要能提前说清楚，而不是等人问了才发现查不到。
        """
        try:
            self.custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
            return True
        except ApiException:
            return False
        except Exception:  # noqa: BLE001
            return False

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

    def chaos_experiments(self, namespace: str) -> list[dict[str, Any]]:
        """列出命名空间里**正在生效**的 Chaos Mesh 实验（只读）。

        为什么要有这个方法，见上面 ``CHAOS_KINDS`` 那段：事件通道会被探针噪音
        和故障自身制造的扰动挤爆、还会过期、还能被清掉；实验对象不会。
        这是"这个命名空间里有没有人在注入故障"的**权威来源**。

        拿不到（没装 Chaos Mesh / 没权限 / CRD 版本不同）一律返回空列表：
        没有 Chaos Mesh 的集群里，这个能力应该静默消失，而不是让排障工具报错。
        """
        out: list[dict[str, Any]] = []
        for kind in CHAOS_KINDS:
            try:
                # ⚠️ 第 4 个位置参数是 **plural（小写）**，不是 Kind。
                # 传 "StressChaos" 会 404，而 404 又被下面的 except 当成
                # "这个 CRD 没装"吞掉——于是功能静默失效，看起来像"集群里没实验"。
                # 这正是本次要修的那类错误（把"没查到"当成"没有"），
                # 所以这里显式小写并留了测试盯着。
                resp = self.custom.list_namespaced_custom_object(
                    CHAOS_GROUP, CHAOS_VERSION, namespace, kind.lower())
            except Exception:  # noqa: BLE001
                # 404 是"这个 CRD 没装"，403 是"没给权限"，都属于正常情况。
                # 其余异常也不该让整个排查查询挂掉。
                continue
            for item in (resp or {}).get("items") or []:
                spec = item.get("spec") or {}
                meta = item.get("metadata") or {}
                status = item.get("status") or {}
                injected = recovered = False
                for cond in status.get("conditions") or []:
                    if cond.get("type") == "AllInjected" and cond.get("status") == "True":
                        injected = True
                    if cond.get("type") == "AllRecovered" and cond.get("status") == "True":
                        recovered = True
                out.append({
                    "kind": kind,
                    "name": meta.get("name", ""),
                    "action": spec.get("action", ""),
                    "detail": _chaos_detail(kind, spec),
                    "mode": spec.get("mode", ""),
                    "duration": spec.get("duration", ""),
                    "created": str(meta.get("creationTimestamp") or ""),
                    # 已恢复的实验对象会留一段时间（duration 到了之后仍存在），
                    # 不能把它当成"故障还在"——那是另一种误导。
                    "active": injected and not recovered,
                    "selector": _chaos_selector_text(spec.get("selector")),
                    "target": _chaos_selector_text(spec.get("target")),
                })
        out.sort(key=lambda x: (x["created"], x["name"]))
        return out

    def chaos_section(self, namespace: str) -> str:
        """把活跃的 Chaos 实验渲染成一段给模型看的文字。

        **没有活跃实验时返回空字符串**——这样把它挂进 get_workload /
        get_events 不会给那 22 个非 chaos 剧本增加任何噪音，也就不会有回归。
        """
        try:
            experiments = self.chaos_experiments(namespace)
        except Exception:  # noqa: BLE001
            return ""
        if not experiments:
            return ""
        active = [e for e in experiments if e["active"]]
        listed = active or experiments
        head = (
            f"⚠️ 这个命名空间里有 {len(active)} 个 Chaos Mesh 实验**正在注入故障**"
            "（这不是猜测：实验对象就在集群里，下面是从对象本身读到的）："
            if active else
            f"注意：这个命名空间里有 {len(experiments)} 个 Chaos Mesh 实验对象，"
            "但状态显示**都已恢复/未注入**——不要把它当成当前故障的原因："
        )
        lines = [head]
        for e in listed:
            bits = [f"{e['kind']}/{e['name']}"]
            if e.get("detail"):
                bits.append(e["detail"])
            elif e["action"]:
                bits.append(f"action={e['action']}")
            if e["selector"]:
                bits.append(f"选择={e['selector']}")
            if e["target"]:
                bits.append(f"对端={e['target']}")
            if e["duration"]:
                bits.append(f"duration={e['duration']}")
            if e["created"]:
                bits.append(f"创建于={e['created'][:19]}")
            bits.append("状态=注入中" if e["active"] else "状态=已恢复")
            lines.append("  " + "  ".join(bits))
        lines.append(
            "  （Chaos 注入的是**运行时**故障：工作负载的 spec、探针、Endpoints "
            "全是正常的。重启/扩容/改配置都撤不掉它——要先处理这个实验对象。）"
        )
        return "\n".join(lines)

    def _read_log(self, pod: str, namespace: str, container: str, tail: int, previous: bool) -> str:
        """读取容器日志，绕开客户端对 text/plain 的错误反序列化。

        kubernetes 客户端把日志端点的响应当作 response_type="str" 处理，会走
        __deserialize_primitive → str(bytes)，得到的是「字节串的 repr」字符串：
        真实换行变成字面量 \\n，非 ASCII 变成 \\xNN 转义。结果是 Agent 读到乱码，
        且注入检测器完全看不到中文。用 _preload_content=False 取原始字节自行解码。
        """
        resp = self.core.read_namespaced_pod_log(
            pod,
            namespace,
            container=container or None,
            tail_lines=tail,
            timestamps=False,
            previous=previous,
            _preload_content=False,
        )
        try:
            return coerce_text(getattr(resp, "data", resp))
        finally:
            release = getattr(resp, "release_conn", None)
            if callable(release):
                release()

    def pod_logs(self, namespace: str, pod: str, container: str = "", tail: int = 100) -> str:
        try:
            return self._read_log(pod, namespace, container, tail, previous=False)
        except ApiException as exc:
            raise K8sUnavailable(f"读取日志失败: {exc.reason}") from exc

    def previous_pod_logs(self, namespace: str, pod: str, container: str = "", tail: int = 50) -> str:
        """读取上一个容器实例的日志——排查 CrashLoopBackOff 的关键。"""
        try:
            return self._read_log(pod, namespace, container, tail, previous=True)
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
            alloc = getattr(n.status, "allocatable", None) or {}
            out.append({
                "name": n.metadata.name,
                "unschedulable": bool(n.spec.unschedulable),
                "labels": dict(n.metadata.labels or {}),
                "conditions": conds,
                "pressured": [k for k, v in conds.items() if v == "True"],
                "taints": [t.key for t in (n.spec.taints or [])],
                # 可分配量用来算利用率，大屏上的百分比就靠它
                "cpu_allocatable": parse_quantity(alloc.get("cpu")),
                "mem_allocatable": parse_quantity(alloc.get("memory")),
                "ready": any(
                    c.type == "Ready" and c.status == "True"
                    for c in ((n.status.conditions or []) if n.status else [])
                ),
            })
        return out

    # ------------------------------------------------------------ 资源用量
    #
    # 数据来自 metrics-server（经 metrics.k8s.io 的 CRD 接口）。
    # 没装 metrics-server 时这个接口不存在，这里**返回空而不是抛异常**——
    # 大屏少一块指标，总好过整页打不开。

    def node_usage(self) -> dict[str, dict[str, Any]]:
        try:
            items = self.custom.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes"
            ).get("items", [])
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict[str, Any]] = {}
        for n in items:
            usage = n.get("usage") or {}
            out[n.get("metadata", {}).get("name", "")] = {
                "cpu_cores": parse_quantity(usage.get("cpu")),
                "mem_bytes": parse_quantity(usage.get("memory")),
            }
        return out

    def pod_usage(self, namespace: str) -> dict[str, dict[str, Any]]:
        """每个 Pod 的当前用量（多容器求和）。"""
        try:
            items = self.custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods"
            ).get("items", [])
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict[str, Any]] = {}
        for p in items:
            cpu = mem = 0.0
            for c in (p.get("containers") or []):
                u = c.get("usage") or {}
                cpu += parse_quantity(u.get("cpu"))
                mem += parse_quantity(u.get("memory"))
            out[p.get("metadata", {}).get("name", "")] = {
                "cpu_cores": cpu, "mem_bytes": mem,
            }
        return out

    def describe_metrics(self, namespace: str, top: int = 10) -> str:
        """给模型看的资源用量摘要：谁在吃内存、离上限还有多远。"""
        usage = self.pod_usage(namespace)
        if not usage:
            return ("拿不到资源指标（metrics-server 可能没装或还没采集到数据）。"
                    "这种情况下不要猜用量，改用重启次数、退出码、事件这些信号。")
        try:
            pods = {p.name: p for p in self.list_pods(namespace)}
        except Exception:  # noqa: BLE001
            pods = {}

        rows = sorted(usage.items(), key=lambda kv: kv[1]["mem_bytes"], reverse=True)
        lines = [f"命名空间 {namespace} 的内存占用前 {min(top, len(rows))} 名："]
        for name, u in rows[:top]:
            limit = parse_quantity(getattr(pods.get(name), "memory_limit", "") or "")
            mem = u["mem_bytes"]
            pct = f"（占上限 {mem / limit * 100:.0f}%）" if limit else ""
            lines.append(
                f"  {name}: 内存 {human_bytes(mem)}{pct}  CPU {u['cpu_cores'] * 1000:.0f}m"
            )

        # 离上限最近的几个最值得看——OOM 往往就出在这里
        near = [
            (name, u["mem_bytes"] / parse_quantity(p.memory_limit))
            for name, u in rows
            if (p := pods.get(name)) is not None
            and parse_quantity(getattr(p, "memory_limit", "") or "") > 0
        ]
        near = [(n, r) for n, r in near if r >= 0.8]
        if near:
            near.sort(key=lambda x: x[1], reverse=True)
            lines.append("⚠️ 已经用掉内存上限 80% 以上的：")
            for n, r in near[:5]:
                lines.append(f"  {n}: {r * 100:.0f}%")

        lines.append("")
        lines.append("节点利用率：")
        nodes = {n["name"]: n for n in self.list_nodes()}
        for name, u in self.node_usage().items():
            n = nodes.get(name, {})
            cpu_a = n.get("cpu_allocatable") or 0
            mem_a = n.get("mem_allocatable") or 0
            cpu_pct = f"{u['cpu_cores'] / cpu_a * 100:.0f}%" if cpu_a else "?"
            mem_pct = f"{u['mem_bytes'] / mem_a * 100:.0f}%" if mem_a else "?"
            lines.append(
                f"  {name}: CPU {cpu_pct}  内存 {mem_pct}"
                + ("（不可调度）" if n.get("unschedulable") else "")
            )
        return "\n".join(lines)

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

    def pods_per_node(self) -> dict[str, int]:
        """每个节点上压了多少 Pod（**全部命名空间**）。

        只统计应用命名空间会漏掉 kube-system 的组件，让控制面节点显示成 0 个 Pod——
        大屏上那是误导，不是简化。
        """
        try:
            resp = self.core.list_pod_for_all_namespaces()
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, int] = {}
        for p in resp.items:
            node = getattr(p.spec, "node_name", None)
            if node:
                out[node] = out.get(node, 0) + 1
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
        if tool == "delete_pvc":
            # 默认锁着：能走到这里说明启动时用 --allow-tool 显式解锁过。
            return self._delete_pvc(ns, name, dry)
        if tool == "cordon_node":
            return self._cordon(params["node"], True, dry)
        if tool == "uncordon_node":
            return self._cordon(params["node"], False, dry)
        if tool == "drain_node":
            return self._drain(params["node"], ns, dry, params)
        if tool == "patch_resources":
            return self._patch_resources(ns, name, params, dry)
        if tool == "patch_service_selector":
            return self._patch_service_selector(ns, params["service"], params["selector"], dry)
        if tool == "patch_service_port":
            return self._patch_service_port(ns, params["service"], params, dry)
        if tool == "patch_probe":
            return self._patch_probe(ns, name, params, dry)
        if tool == "patch_node_selector":
            return self._patch_node_selector(ns, name, params.get("kind", "deployment"), params, dry)
        if tool == "patch_hpa":
            return self._patch_hpa(ns, params["hpa"], params, dry)
        if tool in ("patch_resourcequota", "patch_pdb"):
            # ⚠️ 必须**延迟导入**：capacity.py 反向 import 了本模块
            # （它要用 ActionRejected / K8sClient / parse_quantity），
            # 在文件顶部导入会形成循环导入。
            # 第一次写的时候忘了，干跑直接 NameError——而"干跑"正是审批卡片
            # 上给人看的东西，坏了的话卡片上就是一句报错。
            from . import capacity

            if tool == "patch_resourcequota":
                return capacity.patch_resourcequota(self, ns, name, params, dry)
            return capacity.patch_pdb(self, ns, name, params, dry)
        if tool == "patch_hpa_target":
            return self._patch_hpa_target(ns, params["hpa"], params, dry)
        if tool == "rollback_configmap":
            data = params.get("data")
            if not isinstance(data, dict) or not data:
                raise ActionRejected("rollback_configmap 需要提供要恢复的 data")
            return self.replace_configmap_data(ns, params["name"], data)
        # 变更管理：发布 / 回滚到指定版本 / 金丝雀灰度
        if tool == "patch_image":
            return self._patch_image(ns, name, params.get("kind", "deployment"), params, dry)
        if tool == "patch_configmap":
            return self._patch_configmap(ns, name, params, dry)
        if tool == "rollout_undo_to":
            return self._rollout_undo_to(ns, name, params.get("kind", "deployment"), params, dry)
        if tool == "canary_start":
            return self._canary_start(ns, name, params, dry)
        if tool == "canary_promote":
            return self._canary_promote(ns, name, params, dry)
        if tool == "canary_abort":
            return self._canary_abort(ns, name, params, dry)
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
        dep = self.read_workload(ns, "deployment", name)
        labels = dict(getattr(getattr(dep.spec, "selector", None), "match_labels", None) or {})
        owned = [rs for _, rs in self._owned_replicasets(ns, name, labels)]
        if len(owned) < 2:
            raise ActionRejected("没有可回滚的历史版本（ReplicaSet 少于 2 个）")

        # ⚠️ 回滚目标 = **"非最新"的那个版本**，而不是"replicas==0 的那个"。
        #
        # 曾经用"replicas==0"来识别历史版本，结果是：
        # **新版本崩了导致滚动更新卡住时，新旧两个 ReplicaSet 都还有副本，
        # 于是找不到"非活跃"的 RS → 拒绝回滚。**
        # 而"新版本崩了"恰恰是最需要回滚的时刻——这个判据把最重要的场景堵死了。
        #
        # 正确语义：按创建时间排序，最新的是"当前版本"（无论它健康与否），
        # 回滚目标是其余里最新的那个。
        owned.sort(key=lambda r: r.metadata.creation_timestamp, reverse=True)
        target = owned[1]
        body = {
            "spec": {
                "template": client.ApiClient().sanitize_for_serialization(target.spec.template)
            }
        }
        try:
            self.apps.patch_namespaced_deployment(name, ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"rollout undo 被拒绝: {exc.reason}") from exc

        leftover = (
            [] if dry is not None
            else self._template_rollback_leftover(ns, name, target.spec.template)
        )
        msg = f"已回滚 {name} 到上一版本（来源 ReplicaSet: {target.metadata.name}）"
        return msg + self._leftover_note(leftover)

    # ------------------------------------------------- 变更管理（发布/回滚/灰度）
    #
    # 这一组回答的是"怎么安全地换版本"，而不是"怎么修一个坏掉的对象"。
    # 它延续同一条主张：**能改的字段是枚举出来的**——发布最常见的动作是换镜像，
    # 那就只给"换镜像"这一个入口，而不是顺手开一个能改任意字段的 patch，
    # 更不会引入 apply_manifest（那等于把整条参数化边界让掉）。

    @staticmethod
    def canary_name(name: str) -> str:
        """金丝雀 Deployment 的名字由稳定版名字推导，**不接受调用方指定**。"""
        return f"{name}-canary"

    @staticmethod
    def _containers_of(workload: Any) -> list[Any]:
        """取 pod template 里的容器列表。取不到就是空表，由调用方给出人话错误。"""
        tmpl = getattr(getattr(workload, "spec", None), "template", None)
        spec = getattr(tmpl, "spec", None)
        return list(getattr(spec, "containers", None) or [])

    @staticmethod
    def _json_pointer(key: str) -> str:
        """JSON Pointer 的键名转义（``~`` → ``~0``、``/`` → ``~1``）。

        ConfigMap 的键名约束本身不含这两个字符，但转义一次不花什么代价，
        防的是约束放宽之后出现"键名里带 /，patch 打到别的路径上"这种鬼故事。
        """
        return str(key).replace("~", "~0").replace("/", "~1")

    @staticmethod
    def _revision_of(obj: Any) -> int | None:
        """从对象的 revision 注解里读版本号，读不到返回 None。

        优先认 ``deployment.kubernetes.io/revision``（K8s 控制器写的规范键）；
        兜底再扫一遍任何以 ``/revision`` 结尾的注解键——纯粹是为了容错，
        因为一旦注解键名写错（比如写成 ``rollout.kubernetes.io/revision``），
        症状是"查不到任何历史版本"而不是报错，非常难查。
        """
        annotations = getattr(getattr(obj, "metadata", None), "annotations", None) or {}
        raw = annotations.get(REVISION_ANNOTATION)
        if raw is None:
            for key, value in annotations.items():
                if key.endswith("/revision"):
                    raw = value
                    break
        if raw is None:
            return None
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return None

    def _owned_replicasets(
        self, ns: str, name: str, labels: dict[str, str] | None = None
    ) -> list[tuple[int | None, Any]]:
        """属于 Deployment/{name} 的 ReplicaSet，按 revision 升序返回 ``(revision, rs)``。

        归属判据用 **ownerReference**，而不是 label selector：selector 只说明
        "Pod 可能属于它"，而 ownerReference 是控制器自己写下的归属关系，
        不会因为有人手改标签而漂移（改标签是运维里最常见的操作之一）。
        selector 只用来减少一次列表返回的量。
        """
        kwargs: dict[str, Any] = {}
        if labels:
            selector = ",".join(f"{k}={v}" for k, v in labels.items())
            if selector:
                kwargs["label_selector"] = selector
        try:
            items = self.apps.list_namespaced_replica_set(ns, **kwargs).items
        except ApiException as exc:
            raise ActionRejected(f"读取 ReplicaSet 失败: {exc.reason}") from exc

        owned: list[tuple[int | None, Any]] = []
        for rs in items:
            refs = rs.metadata.owner_references or []
            if not any(ref.kind == "Deployment" and ref.name == name for ref in refs):
                continue
            owned.append((self._revision_of(rs), rs))

        def _sort_key(item: tuple[int | None, Any]):
            # 没有 revision 注解的排最后：它能显示，但**不能**当回滚目标
            rev, rs = item
            ts = getattr(rs.metadata, "creation_timestamp", None)
            return (rev is None, rev if rev is not None else 0, str(ts or ""))

        owned.sort(key=_sort_key)
        return owned

    @staticmethod
    def _current_revision(workload: Any, versions: list[tuple[int | None, Any]]) -> int | None:
        """当前版本号。

        优先用 Deployment 自己身上的 revision 注解，它才是控制器写的
        "我现在是第几版"；"取最大的 revision"只是兜底——某个编号更大的 RS
        可能刚被创建、还没真正接管。
        """
        rev = K8sClient._revision_of(workload)
        if rev is not None:
            return rev
        known = [r for r, _ in versions if r is not None]
        return max(known) if known else None

    def _template_rollback_leftover(
        self, ns: str, name: str, target_template: Any
    ) -> list[str]:
        """回滚后复查：merge patch 删不掉"新模板有、旧模板没有"的字段。

        为什么必须复查：``_rollout_undo`` / ``rollout_undo_to`` 用的是
        **merge patch**，而 merge patch 只能覆盖"新旧模板都有"的键：
          · 改了值（镜像、环境变量的值、探针端口）→ 能滚回去
          · **新增了字段**（加了个 nodeSelector、加了个注解）→ **滚不掉**
        dry-run 只校验请求合法性，**看不出**这个语义差异，所以只能执行后读回来比。
        真实踩到过：pending 剧本（patch 加了 nodeSelector）回滚后 nodeSelector 原封不动，
        操作员看到"已回滚"却发现问题还在。

        抽成 helper 而不是复制两份：这个检查逻辑有两个调用方
        （``rollout_undo`` 和 ``rollout_undo_to``），复制粘贴迟早只修一边。
        """
        leftover: list[str] = []
        try:
            now = self.apps.read_namespaced_deployment(name, ns)
            api = client.ApiClient()
            want = api.sanitize_for_serialization(target_template)
            now_tmpl = getattr(getattr(now, "spec", None), "template", None)
            cur = api.sanitize_for_serialization(now_tmpl) if now_tmpl else {}
            # 只看"新模板多出来的顶层字段"——值的变化是回滚的正常工作
            cur_spec = cur.get("spec") or {}
            want_spec = (want or {}).get("spec") or {}
            for key in cur_spec:
                if key not in want_spec and cur_spec.get(key):
                    leftover.append(f"spec.{key}")
            for c_now, c_want in zip(cur_spec.get("containers") or [],
                                     want_spec.get("containers") or []):
                for key in c_now:
                    if key not in c_want and c_now.get(key):
                        leftover.append(f"container.{c_now.get('name')}.{key}")
        except Exception:  # noqa: BLE001
            pass   # 复查失败不该把回滚本身的结果说成失败
        return leftover

    @staticmethod
    def _leftover_note(leftover: list[str]) -> str:
        if not leftover:
            return ""
        return (
            f"；⚠️ 但回滚**不完整**：{', '.join(sorted(set(leftover))[:4])} "
            f"在新模板里存在、旧模板里没有，merge patch 删不掉它们。"
            f"这些字段需要手工处理（kubectl patch --type=json 用 remove 操作）。"
        )

    def _patch_image(self, ns: str, name: str, kind: str,
                     params: dict[str, Any], dry: str | None) -> str:
        """换指定容器的镜像——"发布新版本"最常用的一件事。

        为什么用 **JSON patch 精确定位到 ``/spec/template/spec/containers/<idx>/image``**，
        而不是整块替换 pod template：
        · 整块替换是拿一份**读到的旧快照**写回去，会顺手抹掉这期间别人对同一个
          template 的改动（并发下这是真会发生的）；
        · merge patch 语义上只能"合并"，表达不了"删掉某个键"，所以整块替换还会
          把上一次没滚干净的字段一起带过去（见 ``_template_rollback_leftover``）。
        只改一个路径，就不会误伤别的字段，也不会掩盖别的改动。
        """
        kind = str(kind or "deployment").lower()
        image = str(params.get("image") or "").strip()
        if not image:
            raise ActionRejected("patch_image 需要 image（新镜像，如 registry/app:v2）")
        if any(ch.isspace() for ch in image):
            raise ActionRejected(f"image 里不能有空白字符：{image!r}")

        cur = self.read_workload(ns, kind, name)
        containers = self._containers_of(cur)
        if not containers:
            raise ActionRejected(f"{kind}/{name} 的 pod template 里没有容器，改不了镜像")

        want = str(params.get("container") or "").strip()
        if want:
            idx = next((i for i, c in enumerate(containers) if c.name == want), None)
            if idx is None:
                # 报出**现有容器名列表**，模型拿到就能自己改对参数重试，
                # 而不是回去猜（这个项目里"错误信息要能直接指导下一步"是硬约定）
                raise ActionRejected(
                    f"{kind}/{name} 里没有名为 {want!r} 的容器；"
                    f"现有容器：{[c.name for c in containers]}。"
                    f"用 container 参数指定其中之一。")
        else:
            # 默认取名为 app 的容器；没有就取第一个——单容器工作负载占绝大多数，
            # 强制调用方先读一遍容器名只会白多一轮往返。
            idx = next((i for i, c in enumerate(containers) if c.name == "app"), 0)

        current = containers[idx].image
        # 原来没有 image 字段时，replace 会因为路径不存在被 422 拒掉，所以按存在与否选 op
        op = "replace" if current else "add"
        self._patch_workload(ns, kind, name, [
            {"op": op,
             "path": f"/spec/template/spec/containers/{idx}/image",
             "value": image},
        ], dry)

        msg = (f"已把 {kind}/{name} 的容器 {containers[idx].name} 镜像从 "
               f"{current or '（无）'} 改成 {image}")
        if dry is None:
            # ⚠️ **执行后复查**：API 接受请求不等于改动落地。
            # 可能被别的控制器/回滚覆盖，也可能操作员点的批准已经过期。
            # 读回来的值才是事实；对不上就如实说，不要给一个假的成功。
            try:
                now = self.read_workload(ns, kind, name)
                got = self._containers_of(now)[idx].image
            except Exception as exc:  # noqa: BLE001
                got = f"<复查失败: {exc}>"
            if got != image:
                msg += (f"；⚠️ 但复查读回的镜像仍是 {got}，这次改动**没有生效**。"
                        f"可能有别的控制器/回滚覆盖了它，请重新核实后再下结论。")
            else:
                msg += "（已复查确认镜像生效）"
        return msg

    def _patch_configmap(self, ns: str, name: str,
                         params: dict[str, Any], dry: str | None) -> str:
        """改 ConfigMap 的键值（新增/覆盖 + 删除键）。

        ⚠️ 带 ``remove`` 时必须走 **JSON patch**：merge patch 是"按 key 合并"的，
        **语法上就删不掉 map 里已有的键**。这和 ``_patch_node_selector`` 里
        "回滚删掉一个 nodeSelector 却删不掉"是同一个坑，照那个模式处理。

        ⚠️ 返回值必须写清"改完不会自己生效"——否则运维会以为改完就好了。
        ConfigMap 只是**配置源**，Pod 不会因为它变了就重启：
          · env / envFrom 注入的值只在容器启动时读一次 → **必须 rollout_restart**；
          · volume 挂载的文件 kubelet 最终会同步，但进程不去重新读文件，
            用的照样是旧值。
        """
        data = {str(k): str(v) for k, v in (params.get("data") or {}).items()}
        remove = [str(k) for k in (params.get("remove") or [])]
        if not data and not remove:
            raise ActionRejected(
                "patch_configmap 至少需要 data（要新增/覆盖的键值）或 remove（要删的键）")

        current = self.read_configmap(ns, name)   # 不存在会抛带说明的 ActionRejected
        unknown = [k for k in remove if k not in current]
        if unknown:
            # 删一个不存在的键，多半是键名打错了——报出来比"删了 0 个键"有用得多
            raise ActionRejected(
                f"ConfigMap/{name} 里没有这些键，删不掉：{unknown}（现有键：{sorted(current)}）")

        if remove:
            ops: list[dict[str, Any]] = []
            if not current:
                # JSON patch 不能往一个不存在的父路径下加键，先补出 /data
                ops.append({"op": "add", "path": "/data", "value": {}})
            for k, v in data.items():
                # add 对已存在的成员就是替换，所以增改都用 add，不必区分
                ops.append({"op": "add", "path": f"/data/{self._json_pointer(k)}", "value": v})
            for k in remove:
                ops.append({"op": "remove", "path": f"/data/{self._json_pointer(k)}"})
            body: Any = ops
            kwargs: dict[str, Any] = {"_content_type": "application/json-patch+json"}
        else:
            body = {"data": data}
            kwargs = {}

        try:
            self.core.patch_namespaced_config_map(name, ns, body, dry_run=dry, **kwargs)
        except ApiException as exc:
            raise ActionRejected(f"修改 ConfigMap 被拒绝: {exc.reason} {exc.body}") from exc

        parts = []
        if data:
            parts.append(f"设置 {sorted(data)}")
        if remove:
            parts.append(f"删除 {remove}")
        return (
            f"已把 {'，'.join(parts)} 写入 ConfigMap/{name}。"
            f"⚠️ 这**不会**重启任何 Pod：env/envFrom 注入的值必须 "
            f"rollout_restart 才会被读到；volume 挂载的文件 kubelet 会最终同步，"
            f"但进程不重新读取文件就还是旧值。"
        )

    def _rollout_undo_to(self, ns: str, name: str, kind: str,
                         params: dict[str, Any], dry: str | None) -> str:
        """回滚到**指定 revision** 的 pod template。

        与 ``rollout_undo``（只看上一版）的区别是目标由调用方指定，
        所以它**依赖 ``rollout_history`` 先能列出有哪些版本**——不知道版本号就无从指定。

        找目标 RS 用控制器写的 revision 注解；找不到时**列出所有可用 revision**，
        而不是干巴巴一句"没找到"：模型拿到列表就能改对参数重试。
        """
        kind = str(kind or "deployment").lower()
        if kind != "deployment":
            raise ActionRejected(
                f"rollout_undo_to 只支持 deployment（{kind} 的历史不挂在 ReplicaSet 上）")
        if params.get("revision") is None:
            raise ActionRejected("rollout_undo_to 需要 revision（整数，用 rollout_history 查）")
        try:
            revision = int(params["revision"])
        except (TypeError, ValueError):
            raise ActionRejected("rollout_undo_to 的 revision 必须是整数") from None

        dep = self.read_workload(ns, kind, name)
        labels = dict(getattr(getattr(dep.spec, "selector", None), "match_labels", None) or {})
        versions = self._owned_replicasets(ns, name, labels)
        available = [r for r, _ in versions if r is not None]
        target = next((rs for r, rs in versions if r == revision), None)
        if target is None:
            raise ActionRejected(
                f"Deployment/{name} 里没有 revision={revision} 的版本；"
                f"现有可用 revision：{available or '（一个都没有）'}。"
                f"用 rollout_history 看每个版本对应的镜像和副本数。")

        body = {
            "spec": {
                "template": client.ApiClient().sanitize_for_serialization(target.spec.template)
            }
        }
        try:
            self.apps.patch_namespaced_deployment(name, ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"回滚到 revision={revision} 被拒绝: {exc.reason} {exc.body}") from exc

        # 与 rollout_undo 共用同一段复查逻辑（见 _template_rollback_leftover 的说明）
        leftover = (
            [] if dry is not None
            else self._template_rollback_leftover(ns, name, target.spec.template)
        )
        msg = (f"已把 Deployment/{name} 回滚到 revision={revision}"
               f"（来源 ReplicaSet: {target.metadata.name}）")
        return msg + self._leftover_note(leftover)

    def _deployment_exists(self, ns: str, name: str) -> bool:
        try:
            self.apps.read_namespaced_deployment(name, ns)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise ActionRejected(f"读取 Deployment/{name} 失败: {exc.reason}") from exc

    def _canary_start(self, ns: str, name: str,
                      params: dict[str, Any], dry: str | None) -> str:
        """起一个金丝雀 Deployment：从稳定版**克隆** pod template，只改镜像。

        为什么金丝雀名字**固定**为 ``<name>-canary``，不接受调用方指定名字：
        这个工具的安全边界就是"只能创建一个和稳定版配对的金丝雀"。一旦名字可以由
        参数决定，``canary_promote`` / ``canary_abort`` 就得先反问"你说的是哪个金丝雀"，
        而模型完全可以把名字指到**别的**工作负载上——那不是灰度，是误操作。
        名字由输入推导出来，动作就不可能落到工具作者没想到的对象上。

        为什么标签要和稳定版**完全一致**（只额外加一个 ``omagent-role: canary``）：
        灰度是按 **Service 的就绪副本数比例**分配的——Service 的 selector 同时选中
        稳定版和金丝雀的 Pod，kube-proxy 按 Endpoints 里的就绪 Pod 轮询，
        所以 3:1 的副本比 ≈ 25% 的流量给金丝雀。给金丝雀换一套独立标签就做不到
        "两个版本同时收流量"，那变成了切流而不是灰度。
        """
        kind = str(params.get("kind") or "deployment").lower()
        if kind != "deployment":
            raise ActionRejected(f"canary_start 只支持 deployment，收到 {kind!r}")
        image = str(params.get("image") or "").strip()
        if not image:
            raise ActionRejected("canary_start 需要 image（金丝雀版本要跑的镜像）")
        if params.get("replicas") is None:
            raise ActionRejected("canary_start 需要 replicas（金丝雀起几个副本，至少 1）")
        try:
            replicas = int(params["replicas"])
        except (TypeError, ValueError):
            raise ActionRejected("canary_start 的 replicas 必须是整数") from None
        if replicas < 1:
            raise ActionRejected(
                "金丝雀副本数必须 >= 1；0 副本等于没起（想撤掉金丝雀用 canary_abort）")

        stable = self.read_workload(ns, "deployment", name)   # 不存在 → ActionRejected
        canary_name = self.canary_name(name)
        if self._deployment_exists(ns, canary_name):
            # 不覆盖：静默覆盖会把上一次灰度还在观察中的金丝雀换掉，观察者却以为没变
            raise ActionRejected(
                f"Deployment/{canary_name} 已经存在，不覆盖。"
                f"要把它转正用 canary_promote，要撤掉用 canary_abort。")

        containers = self._containers_of(stable)
        if not containers:
            raise ActionRejected(f"Deployment/{name} 的 pod template 里没有容器，无法克隆")
        want = str(params.get("container") or "").strip()
        if want:
            idx = next((i for i, c in enumerate(containers) if c.name == want), None)
            if idx is None:
                raise ActionRejected(
                    f"Deployment/{name} 里没有名为 {want!r} 的容器；"
                    f"现有容器：{[c.name for c in containers]}")
        else:
            idx = next((i for i, c in enumerate(containers) if c.name == "app"), 0)

        # 克隆 pod template：先序列化再改，改的是**副本**，稳定版对象一个字都不动
        template = client.ApiClient().sanitize_for_serialization(stable.spec.template) or {}
        tmpl_spec = template.get("spec") or {}
        tmpl_containers = tmpl_spec.get("containers") or []
        if idx >= len(tmpl_containers):
            raise ActionRejected(f"克隆 {name} 的 pod template 失败：容器下标越界")
        tmpl_containers[idx]["image"] = image
        tmpl_spec["containers"] = tmpl_containers
        template["spec"] = tmpl_spec

        stable_labels = dict(
            getattr(getattr(getattr(stable.spec, "template", None), "metadata", None),
                    "labels", None) or {})
        if not stable_labels:
            raise ActionRejected(
                f"Deployment/{name} 的 pod 模板没有标签，无法保证 Service 同时选中两个版本")
        labels = {**stable_labels, "omagent-role": "canary"}
        meta = template.get("metadata") or {}
        meta["labels"] = labels
        # 服务端字段留着只会让 create 变脆（某些版本会直接报"不该设置的字段"）
        for junk in ("creationTimestamp", "resourceVersion", "uid", "managedFields"):
            meta.pop(junk, None)
        template["metadata"] = meta

        selector = client.ApiClient().sanitize_for_serialization(stable.spec.selector) or {}
        body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": canary_name,
                "namespace": ns,
                # Deployment 自己的标签也带上稳定版的那套，便于按应用名一起检索
                "labels": {**dict(stable.metadata.labels or {}), "omagent-role": "canary"},
            },
            "spec": {"replicas": replicas, "selector": selector, "template": template},
        }
        try:
            self.apps.create_namespaced_deployment(ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"创建金丝雀被拒绝: {exc.reason} {exc.body}") from exc

        stable_replicas = stable.spec.replicas or 0
        total = stable_replicas + replicas
        share = f"{replicas / total * 100:.0f}%" if total else "?"
        return (
            f"已创建金丝雀 Deployment/{canary_name}：镜像={image} 副本={replicas}；"
            f"稳定版 {name} 未改动。"
            f"⚠️ 这是按**就绪副本数**加权的灰度，不是按请求数加权：Service 的 selector "
            f"同时选中两个版本的 Pod，流量按 Endpoints 里的就绪副本数分配——"
            f"当前稳定版 {stable_replicas} : 金丝雀 {replicas}，约 {share} 的流量会到金丝雀"
            f"（前提是金丝雀 Pod 真的 Ready，没 Ready 就不会进 Endpoints）。"
            f"观察好之后用 canary_promote 转正，不行就用 canary_abort 撤掉。"
        )

    def _canary_promote(self, ns: str, name: str,
                        params: dict[str, Any], dry: str | None) -> str:
        """金丝雀转正：把金丝雀的镜像发布到稳定版，再把金丝雀缩到 0。

        为什么"转正"= 把镜像 patch 到稳定版，而不是"把稳定版扩、金丝雀缩"或者
        "改 Service selector 指向金丝雀"：后两种做法会让"线上到底跑的是哪个版本"
        变成一个只能靠查 Pod 才能回答的问题，而且下一轮灰度就没有干净的稳定版基线了。
        稳定版 Deployment 始终是**唯一的事实来源**，金丝雀只是它的一次预演。

        容器按**名字**配对，不按下标：金丝雀是从稳定版克隆的，但只要中途有人调整过
        容器顺序，按下标就会把镜像搬到错误的容器上。
        """
        canary_name = self.canary_name(name)
        try:
            canary = self.apps.read_namespaced_deployment(canary_name, ns)
        except ApiException as exc:
            if exc.status == 404:
                raise ActionRejected(
                    f"没有找到金丝雀 Deployment/{canary_name}——先用 canary_start 起一个") from exc
            raise ActionRejected(f"读取金丝雀 Deployment/{canary_name} 失败: {exc.reason}") from exc

        stable = self.read_workload(ns, "deployment", name)   # 不存在 → ActionRejected
        canary_images = {c.name: c.image for c in self._containers_of(canary)}
        stable_containers = self._containers_of(stable)
        matched = [c for c in stable_containers if c.name in canary_images]
        if not matched:
            raise ActionRejected(
                f"金丝雀 {canary_name} 的容器名 {sorted(canary_images)} 和稳定版 "
                f"{[c.name for c in stable_containers]} 对不上，无法确定要发布哪个镜像")

        ops: list[dict[str, Any]] = []
        applied: list[str] = []
        for i, c in enumerate(stable_containers):
            image = canary_images.get(c.name)
            if not image or image == c.image:
                continue
            ops.append({"op": "replace" if c.image else "add",
                        "path": f"/spec/template/spec/containers/{i}/image",
                        "value": image})
            applied.append(f"{c.name}: {c.image or '（无）'} → {image}")

        if ops:
            self._patch_workload(ns, "deployment", name, ops, dry)
        # 金丝雀缩到 0：改的是 scale 子资源，不是 pod template——
        # 改 template 会顺手触发一次没必要的滚动更新。
        try:
            self.apps.patch_namespaced_deployment_scale(
                canary_name, ns, {"spec": {"replicas": 0}}, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"把金丝雀缩容到 0 被拒绝: {exc.reason}") from exc

        if applied:
            head = f"已把金丝雀的镜像发布到 Deployment/{name}（" + "；".join(applied) + "）"
        else:
            head = f"金丝雀与 Deployment/{name} 的镜像已经一致，本次没有需要发布的改动"
        return (
            head + f"；金丝雀 {canary_name} 已缩容到 0 副本。"
            f"稳定版的滚动更新由它自己接管——收工前确认新版本 Pod 全部 Ready、"
            f"旧版本 Pod 已经被替换掉。"
        )

    def _canary_abort(self, ns: str, name: str,
                      params: dict[str, Any], dry: str | None) -> str:
        """放弃金丝雀：把 ``<name>-canary`` 缩到 0，**稳定版一个字都不动**。

        为什么只缩到 0、不删对象：删除不可逆（而且删除的 dry-run 什么都不校验——
        见 ``_delete_pvc`` 的说明），而缩到 0 已经达成了"流量全部回稳定版"的全部目的，
        还留着 Deployment 记录供人回看这次灰度试过什么镜像。
        真要删，那是人的决定，让他在终端里自己删。
        """
        canary_name = self.canary_name(name)
        if not self._deployment_exists(ns, canary_name):
            raise ActionRejected(
                f"没有找到金丝雀 Deployment/{canary_name}，没有可放弃的金丝雀"
                f"（用 canary_start 起金丝雀；稳定版 {name} 不在本工具的作用范围内）")
        try:
            self.apps.patch_namespaced_deployment_scale(
                canary_name, ns, {"spec": {"replicas": 0}}, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"把金丝雀缩容到 0 被拒绝: {exc.reason}") from exc
        return (
            f"已把金丝雀 Deployment/{canary_name} 缩容到 0 副本——"
            f"**稳定版 {name} 没有被改动**，流量全部回到稳定版。"
            f"金丝雀对象保留着（便于回看试过的镜像）；要彻底清掉请人工执行 "
            f"kubectl delete deployment {canary_name} -n {ns}。"
        )


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

    def _delete_pvc(self, ns: str, name: str, dry: str | None) -> str:
        """删除一个 PVC。**只有 ``--allow-tool delete_pvc`` 解锁之后才可能走到这里。**

        ⚠️ 这个动作的 dry-run **不提供任何保护**，必须写清楚免得有人误以为
        干跑一下就有安全感：服务端对 delete 的 dry-run 只校验请求格式，
        不会告诉你"这个 PV 会被回收、数据没了"，也不会拦"PVC 正被 Pod 使用"。
        （实测过：``delete pvc --dry-run=server`` 对这些情况照样返回成功。）

        所以它的安全边界**不在 dry-run 上**，而在这三处：
        1. 默认锁着，必须启动时 ``--allow-tool`` 显式解锁；
        2. ``danger="severe"`` → 前端要求**手输 PVC 名字**才能批准；
        3. 走同一条审批 + 审计链，谁批的、什么时候批的都留痕。
        """
        if not name:
            raise ActionRejected("delete_pvc 需要指定 name")
        try:
            self.core.delete_namespaced_persistent_volume_claim(name, ns, dry_run=dry)
        except ApiException as exc:
            if exc.status == 404:
                raise ActionRejected(f"命名空间 {ns} 下没有 PVC/{name}") from exc
            raise ActionRejected(f"删除 PVC 被拒绝: {exc.reason}") from exc
        return f"已删除 PVC/{name}（命名空间 {ns}）——这是不可逆操作"

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

    def _patch_hpa_target(self, ns: str, name: str, params: dict[str, Any],
                          dry: str | None) -> str:
        """改 HPA 的 ``scaleTargetRef``——也就是"它到底在给谁扩缩容"。

        为什么要有这个工具：``patch_hpa`` 只能改 min/maxReplicas。而
        ``scaleTargetRef`` 一旦被指到一个不存在的名字，HPA 会一直报
        ``FailedGetScale``，**副本数完全不动，min/max 改多少次都没用**——
        改区间治不了改坏的目标。此前这个场景的正解只能是
        "我没有工具，请你手工执行"。

        ⚠️ 这是本项目里**危险级别最高的一类改动**：指错对象就等于让 HPA
        去扩缩容另一个工作负载。三道约束：
        - 必须先读（``get_hpa``）确认目标真的坏了——写工具都有对应读工具；
        - **整块替换** ``scaleTargetRef``，不做局部 patch（留一半旧值更危险）；
        - server-side dry-run 会真的让 API server 校验这个目标能不能扩缩，
          这点和 delete 不同（delete 的 dry-run 不校验任何东西）。
        """
        target = str(params.get("target") or "").strip()
        if not target:
            raise ActionRejected("target 不能为空——那会让 HPA 失去扩缩容对象")
        kind = str(params.get("kind") or "Deployment").strip() or "Deployment"
        api_version = str(params.get("api_version") or "apps/v1").strip() or "apps/v1"

        body = {
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": api_version,
                    "kind": kind,
                    "name": target,
                }
            }
        }
        try:
            self.autoscaling_v2.patch_namespaced_horizontal_pod_autoscaler(
                name, ns, body, dry_run=dry)
        except ApiException as exc:
            raise ActionRejected(f"改 HPA 扩缩容目标被拒绝: {exc.reason}") from exc
        return f"已把 HPA/{name} 的扩缩容目标改成 {kind}/{target}"

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
            "get_hpa": self._q_hpa,
            "get_resourcequota": self._q_resourcequota,
            # 容量与成本：实现在 capacity.py，这里只做转发。
            # 单独一个模块是因为那是自成一套的计算（可分配 → 预留 → 用量 → 单价），
            # 而 k8s.py 已经两千多行。
            "get_capacity": self._q_capacity,
            "get_cost": self._q_cost,
            "get_replicasets": self._q_replicasets,
            "rollout_history": self._q_rollout_history,
            "get_pvc": self._q_pvc,
            "get_metrics": self._q_metrics,
        }.get(tool)
        if handler is None:
            raise ActionRejected(f"{tool!r} 不是已实现的只读诊断动作")
        return handler(ns, params)

    def _q_metrics(self, ns: str, params: dict[str, Any]) -> str:
        """资源用量：谁在吃内存、离上限还有多远、节点忙不忙。"""
        return self.describe_metrics(ns)

    def _q_hpa(self, ns: str, params: dict[str, Any]) -> str:
        """读取 HPA 的区间与**状态条件**。

        为什么必须有这个工具：``patch_hpa`` 是写工具，而在此之前没有任何
        读 HPA 的只读工具——于是出现"改得了一个自己读不了的 HPA"这种荒谬局面。
        模型只能从事件里猜，而集群里任何一条跟 HPA 沾边的噪音事件都会把它带偏
        （真实发生过：它把"没装 metrics-server"这条**一直存在**的环境噪音
        当成了刚发生的故障根因）。

        ⚠️ 必须用 v2 API：v1 的 status 对象上根本没有 ``conditions`` 属性。
        """
        name = params.get("name", "")
        try:
            if name:
                items = [self.autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name, ns)]
            else:
                items = list(self.autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(ns).items)
        except ApiException as exc:
            if exc.status == 404:
                return f"命名空间 {ns} 下没有 HPA/{name}"
            raise ActionRejected(f"读取 HPA 失败: {exc.reason}") from exc

        if not items:
            return f"命名空间 {ns} 下没有 HorizontalPodAutoscaler"

        out = []
        for h in items:
            tgt = getattr(h.spec.scale_target_ref, "name", "?")
            kind = getattr(h.spec.scale_target_ref, "kind", "?")
            status = h.status
            out.append(
                f"HPA/{h.metadata.name}: 目标={kind}/{tgt} "
                f"区间=[{h.spec.min_replicas}, {h.spec.max_replicas}] "
                f"当前副本={getattr(status, 'current_replicas', '?')} "
                f"期望副本={getattr(status, 'desired_replicas', '?')}"
            )
            for m in (h.spec.metrics or []):
                res = getattr(m, "resource", None)
                if res is not None:
                    out.append(
                        f"  指标: {res.name} 目标利用率="
                        f"{getattr(getattr(res, 'target', None), 'average_utilization', '?')}%"
                    )
            for c in ((status.conditions or []) if status else []):
                # 只把**异常**的条件标出来，正常的不刷屏
                if c.status == "True" and c.type not in ("ScalingLimited",):
                    out.append(f"  ✓ {c.type}: {c.reason}")
                elif c.status == "False":
                    out.append(f"  ✗ {c.type}=False: {c.reason} — {(c.message or '')[:200]}")
        return "\n".join(out)

    def _q_resourcequota(self, ns: str, params: dict[str, Any]) -> str:
        """读取 ResourceQuota 的硬上限与已用量——判断"Pod 创建不出来"是否卡在配额。"""
        quotas = self.list_resourcequotas(ns)
        if not quotas:
            return f"命名空间 {ns} 下没有 ResourceQuota"
        out = []
        for q in quotas:
            out.append(f"ResourceQuota/{q['name']}:")
            for k, hard in sorted(q["hard"].items()):
                used = q["used"].get(k, "-")
                flag = "  ⚠️ 已用满" if k in q["exhausted"] else ""
                out.append(f"  {k}: 已用 {used} / 上限 {hard}{flag}")
        return "\n".join(out)

    def _q_replicasets(self, ns: str, params: dict[str, Any]) -> str:
        """列出 ReplicaSet 及其所属工作负载与版本。

        ``rollout_undo`` 依赖 ReplicaSet 历史（少于 2 个就没法回滚），
        在此之前模型看不到有几个历史版本，只能靠猜。
        """
        name = params.get("name", "")
        try:
            items = self.apps.list_namespaced_replica_set(ns).items
        except ApiException as exc:
            raise ActionRejected(f"读取 ReplicaSet 失败: {exc.reason}") from exc

        rows = []
        for rs in items:
            owner = next(
                (o.name for o in (rs.metadata.owner_references or []) if o.kind == "Deployment"),
                "",
            )
            if name and owner != name:
                continue
            rev = (rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
            img = ""
            tmpl = getattr(rs.spec, "template", None)
            if tmpl is not None and tmpl.spec.containers:
                img = tmpl.spec.containers[0].image
            rows.append(
                f"ReplicaSet/{rs.metadata.name} 属于={owner or '-'} 版本={rev} "
                f"期望={rs.spec.replicas} 就绪={rs.status.ready_replicas or 0} 镜像={img}"
            )
        if not rows:
            return f"{('Deployment/' + name) if name else '命名空间 ' + ns} 下没有匹配的 ReplicaSet"
        rows.sort()
        return "\n".join(rows)

    def _q_capacity(self, ns: str, params: dict[str, Any]) -> str:
        """集群余量。实现见 ``capacity.describe_capacity``。"""
        from . import capacity

        return capacity.describe_capacity(self, ns)

    def _q_cost(self, ns: str, params: dict[str, Any]) -> str:
        """成本估算。实现见 ``capacity.describe_cost``。"""
        from . import capacity

        return capacity.describe_cost(self, ns)

    def _q_rollout_history(self, ns: str, params: dict[str, Any]) -> str:
        """列出可回滚的历史版本（revision / 镜像 / 副本数 / 创建时间 / 是否当前）。

        这个只读工具是 ``rollout_undo_to`` 的**前提**：不知道有哪些 revision，
        指定回滚目标就只能靠猜，而猜错版本比不回滚更糟。所以它不只是"多一个读工具"，
        而是那条写路径能不能安全走通的一个组成部分。
        """
        kind = str(params.get("kind") or "deployment").lower()
        name = params.get("name", "")
        if not name:
            raise ActionRejected("rollout_history 需要 name")
        workload = self.read_workload(ns, kind, name)   # 不存在 → ActionRejected

        if kind != "deployment":
            # StatefulSet 的版本不挂在 ReplicaSet 上（它用 ControllerRevision），
            # 编不出"可回滚版本"就别硬编——如实说清楚，免得模型拿到一个空列表
            # 就以为"这个工作负载从来没有发布过"。
            return (f"{kind}/{name} 没有 ReplicaSet 历史（它的版本挂在 ControllerRevision 上），"
                    f"rollout_history 目前只能列出 Deployment 的可回滚版本。")

        labels = dict(getattr(getattr(workload.spec, "selector", None), "match_labels", None) or {})
        versions = self._owned_replicasets(ns, name, labels)
        if not versions:
            return (f"Deployment/{name} 没有可回滚的历史版本"
                    f"（找不到任何 ownerReference 指向它的 ReplicaSet）")

        current = self._current_revision(workload, versions)
        lines = [f"Deployment/{name} 共 {len(versions)} 个可回滚版本（按 revision 升序）："]
        for revision, rs in versions:
            tmpl = getattr(rs.spec, "template", None)
            images = [c.image for c in (getattr(getattr(tmpl, "spec", None), "containers", None) or [])]
            desired = rs.spec.replicas
            ready = getattr(rs.status, "ready_replicas", None) or 0
            created = getattr(rs.metadata, "creation_timestamp", None)
            mark = "  ← 当前版本" if revision is not None and revision == current else ""
            lines.append(
                f"  revision={revision if revision is not None else '?'} "
                f"镜像={images or ['?']} 期望副本={desired} 就绪={ready} "
                f"创建于={created} ReplicaSet={rs.metadata.name}{mark}")
        lines.append("要回滚到其中某一版，用 rollout_undo_to 并传上面的 revision 数字。")
        return "\n".join(lines)

    def _q_pvc(self, ns: str, params: dict[str, Any]) -> str:
        """列出 PVC 的绑定状态与容量——有状态服务出问题时必看。"""
        name = params.get("name", "")
        try:
            items = self.core.list_namespaced_persistent_volume_claim(ns).items
        except ApiException as exc:
            raise ActionRejected(f"读取 PVC 失败: {exc.reason}") from exc
        out = []
        for c in items:
            if name and c.metadata.name != name:
                continue
            cap = (c.status.capacity or {}).get("storage", "?") if c.status else "?"
            sc = getattr(c.spec, "storage_class_name", "") or "-"
            out.append(
                f"PVC/{c.metadata.name} 状态={getattr(c.status, 'phase', '?')} "
                f"容量={cap} storageClass={sc} volume={getattr(c.spec, 'volume_name', '') or '-'}"
            )
        if not out:
            return f"{('PVC/' + name) if name else '命名空间 ' + ns} 下没有 PersistentVolumeClaim"
        return "\n".join(out)

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
        """读事件。

        ⚠️ 两件事必须同时做到，否则模型会把"我没看到"当成"没有发生"：

        1. **说清楚被截断了多少**（原来只有这一条）。
        2. **别让单一 reason 吃光窗口**。实测（2026-10-16）：demo 命名空间里
           busybox `nc` 单连接模型常年刷新 42 条 `Unhealthy` 探针失败事件，
           光这一个 reason 就超过 15 条的窗口；StressChaos 注入后 HPA 又扩了
           2 个副本，多出约 30 条更新的事件，结果 `StressChaos/om-stress-cpu`
           的 8 条事件被挤到**第 33~40 位**，模型完全看不到——于是把
           "CPU 被人打满" 误判成 "CPU 限额设小了"，提了 patch_resources。
           现在按 reason 去重（同一 reason 最多 3 条），稀有 reason 才有机会露头。

        ⚠️ 但**去重只是缓解，不是根治**：事件会过期、还能被人清空
        （"静默注入"就是这么干的：实验对象还在，命名空间事件被清空，
         模型读到"没有相关事件"）。所以活跃的 Chaos 实验**另走对象直读**，
        见 ``chaos_section``——那段不受窗口大小和 TTL 影响。
        """
        events = self.list_events(ns, involved_name=params.get("name", ""))
        lines: list[str] = []
        if events:
            shown, folded = _dedupe_by_reason(events, limit=15, per_reason=3)
            lines += [
                f"[{e['type']}] {e['reason']} {e['object']}: {e['message'][:200]}"
                for e in shown
            ]
            if len(events) > len(shown):
                extra = ""
                if folded:
                    extra = ("（同因折叠：" +
                             "、".join(f"{r}×{n}" for r, n in folded[:5]) + "）")
                lines.append(
                    f"（以上是最近 {len(shown)} 条，该范围共有 {len(events)} 条事件。"
                    f"{extra}"
                    f"要缩小范围可以传 name 只看某个对象——"
                    f"**不要把'这里没看到'当成'没有发生'**）"
                )
        else:
            lines.append(f"命名空间 {ns} 下没有相关事件")

        chaos = self.chaos_section(ns)
        if chaos:
            lines.append(chaos)
        return "\n".join(lines)

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
            # 把"最近一次发布改了什么"直接附在这里。
            #
            # 为什么要塞进来，而不是单独做一个工具：实测加了一个
            # get_recent_changes 工具，跑了 15 个场景**一次都没被调用**——
            # 模型在 29 个工具里沿用训练里的 kubectl 式路径，不会主动试新工具名。
            # 而 get_workload 几乎每个场景都会调，信息放在这里**不依赖它的自觉**。
            #
            # 局部 import 避免循环依赖（changehistory 依赖本模块）。
            try:
                from .changehistory import summarize_recent_changes

                extra = summarize_recent_changes(self, ns, kind, name)
                if extra:
                    out.append(extra)
            except Exception:  # noqa: BLE001
                pass   # 变更摘要拿不到不该让整个查询失败
        # 活跃的 Chaos 实验也挂在这里——和上面"最近变更"是同一个理由：
        # get_workload 几乎每个剧本都会调，信息放在这里**不依赖模型的自觉**。
        # 实测教训：stress 的 3 次里有一次**从头到尾没调过 get_events**
        # （它读了 metrics/日志/ConfigMap 就下了结论），事件通道里再全也白搭。
        chaos = self.chaos_section(ns)
        if chaos:
            out.append(chaos)
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
