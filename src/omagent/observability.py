"""可观测性数据源：Prometheus 与 Loki。

## 为什么这两个是重点

`docs/竞品对比.md` 里最扎心的一条是：HolmesGPT 有 40+ 数据源集成，
而这个项目**只有 K8s 原生 API**。但现实中"Pod 为什么没起来"经常要看两样东西：

- **指标**（Prometheus）——重启趋势、资源水位、可用副本数随时间怎么变的
- **日志**（Loki）——K8s 原生 `get_logs` 只能读**当前** Pod 的日志，
  而排查 CrashLoopBackOff 往往需要**上一个**实例的、或者**跨 Pod 聚合**的日志

K8s API 只能告诉你"此刻是什么样"，指标和日志才能告诉你"怎么变成这样的"。
所以这两个 provider 补的不是"功能数量"，是**取证的时间维度**。

## 怎么连上去：优先走 API Server 的 service proxy

最省事的做法本来应该是配一个 Prometheus 的 URL。但这个项目跑在你的 kubeconfig
上，而 Prometheus 通常是 ClusterIP——从宿主机根本连不上，你得额外开
port-forward 或者配 ingress，等于给"装了就能用"又加一道门槛。

所以默认走 **API Server 的 service proxy**：
``/api/v1/namespaces/<ns>/services/<name>:<port>/proxy/<path>``。
它复用已有的 kubeconfig 凭据，不需要任何额外网络配置。生产环境想直连
Service DNS 的话，传 ``url=`` 覆盖即可。

⚠️ 两个踩过的坑，都写进代码了：
1. **name 必须带端口**（``prometheus:9090``）。只给服务名的话 API Server 会
   报 ``no endpoints available``，而 Service 的 Endpoints 明明是好的——
   排查起来非常浪费时间。
2. ``connect_get_namespaced_service_proxy_with_path`` **不能用来传查询串**：
   它会把 ``?`` 也转义进路径，Prometheus 直接 404。必须走底层
   ``api_client.call_api(..., query_params=[...])``。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

import requests

from .k8s import K8sClient, coerce_text
from .providers import Provider, ProviderError, ProviderTool
from .tools import ToolSpec

# 裸指标名（Prometheus 的命名规范：字母数字下划线冒号）。
# 只有匹配它才敢在后面拼 {label="v"}。
_BARE_METRIC = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

# 单次查询最多回给模型多少条序列 / 多少行日志。
# 不是为了省事，是为了**不把上下文冲掉**：一个 PromQL 动辄几百条序列，
# 全塞进去会把真正有用的前几轮排查挤出去。
MAX_SERIES = 15
MAX_LOG_LINES = 60


class HttpProvider(Provider):
    """走 HTTP 的数据源基类：要么直连 url，要么走 API Server 代理。"""

    def __init__(
        self,
        k8s: K8sClient,
        namespace: str = "monitoring",
        service: str = "",
        port: int = 0,
        url: str = "",
        timeout: float = 12.0,
    ):
        self.k8s = k8s
        self.namespace = namespace
        self.service = service
        self.port = port
        self.url = url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ 取数

    def _get(self, path: str, params: dict[str, Any] | None = None) -> str:
        """GET 一段文本。直连优先，否则走 service proxy。"""
        if self.url:
            try:
                resp = requests.get(f"{self.url}/{path}", params=params,
                                    timeout=self.timeout)
            except requests.RequestException as exc:
                raise ProviderError(f"连不上 {self.url}：{exc}") from exc
            if resp.status_code >= 400:
                raise ProviderError(f"{self.url} 返回 HTTP {resp.status_code}：{resp.text[:200]}")
            return resp.text

        # 注意 name 里的 ":port"——少了它 API Server 会报 no endpoints available
        resource = (f"/api/v1/namespaces/{self.namespace}/services/"
                    f"{self.service}:{self.port}/proxy/{path}")
        try:
            resp = self.k8s.core.api_client.call_api(
                resource, "GET",
                query_params=[(k, v) for k, v in (params or {}).items()],
                auth_settings=["BearerToken"],
                _preload_content=False, _return_http_data_only=True,
                _request_timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"经 API Server 访问 {self.namespace}/{self.service}:{self.port} 失败："
                f"{type(exc).__name__}: {str(exc)[:160]}"
            ) from exc
        return coerce_text(resp.data)   # 和日志一样：必须自己解码，别指望客户端

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raw = self._get(path, params)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ProviderError(f"返回值不是 JSON：{raw[:200]}") from exc


class PrometheusProvider(HttpProvider):
    """Prometheus：指标取证。

    三个工具对应三类问题：
    - ``query_metrics`` —— "它现在什么水位/趋势"（任意 PromQL）
    - ``get_alerts``    —— "有没有相关告警正在响"（比人肉翻指标快得多）
    - ``get_targets``   —— "为什么查不到指标"（抓取目标挂了的话，
      指标缺失本身就可能是根因，不能让它变成"查了个空"就结束）
    """

    name = "prometheus"

    def __init__(self, k8s: K8sClient, *, namespace: str = "monitoring",
                 service: str = "prometheus", port: int = 9090, url: str = ""):
        super().__init__(k8s, namespace=namespace, service=service,
                         port=port, url=url)

    def available(self) -> tuple[bool, str]:
        try:
            d = self._get_json("api/v1/status/buildinfo")
        except ProviderError as exc:
            return False, str(exc)[:200]
        ver = (d.get("data") or {}).get("version", "?")
        return True, f"Prometheus {ver}"

    def tools(self) -> list[ProviderTool]:
        return [
            ProviderTool(
                ToolSpec(
                    "query_metrics", False,
                    "用 PromQL 查指标。适合看趋势、水位、可用副本数——"
                    "K8s 原生工具只能看到「此刻」，指标能看到「怎么变成这样的」。"
                    "常用：kube_pod_container_status_restarts_total（重启次数）、"
                    "kube_deployment_status_replicas_available（可用副本）、"
                    "kube_pod_status_phase{phase=\"Pending\"}、"
                    "container_memory_working_set_bytes{namespace=\"demo\"}",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "PromQL 表达式"},
                            "namespace": {"type": "string",
                                          "description": "可选。给了就在表达式外加一层 namespace 过滤"},
                        },
                        "required": ["query"],
                    },
                ),
                self._query_metrics,
            ),
            ProviderTool(
                ToolSpec("get_alerts", False,
                         "列出 Prometheus 里正在 firing/pending 的告警。"
                         "排查前先看一眼，能省掉大量猜想。",
                         {"type": "object", "properties": {}}),
                self._get_alerts,
            ),
            ProviderTool(
                ToolSpec("get_targets", False,
                         "列出 Prometheus 的抓取目标及其健康状态。"
                         "指标查不到时用它定位是不是抓取本身挂了。",
                         {"type": "object", "properties": {}}),
                self._get_targets,
            ),
        ]

    # ------------------------------------------------------------------ 实现

    def _query_metrics(self, params: dict[str, Any]) -> str:
        expr = str(params.get("query", "")).strip()
        if not expr:
            raise ProviderError("query 不能为空")
        ns = str(params.get("namespace", "")).strip()
        # 只在表达式**就是一个裸指标名**时才自动加 namespace 过滤。
        #
        # 两个坑：
        # 1. PromQL 的标签名**不能加引号**。写成 {"namespace":"demo"} 会直接
        #    语法错误（parse error: unexpected character inside braces: ':'），
        #    正确写法是 {namespace="demo"}。
        # 2. 对 rate(x[5m]) 这种函数/聚合表达式，往后拼一个 {} 是非法的。
        #    拼错了比不拼更糟——模型会拿到一个语法错误，然后浪费一轮去猜。
        #    所以只对裸指标名动手，其余原样交给模型。
        if ns and _BARE_METRIC.match(expr):
            expr = f'{expr}{{namespace="{ns}"}}'

        d = self._get_json("api/v1/query", {"query": expr})
        if d.get("status") != "success":
            raise ProviderError(f"PromQL 执行失败：{d.get('error', d)}")

        result = d["data"]["result"]
        if not result:
            return f"没有数据。表达式：{expr}\n（可能是该指标不存在，或抓取目标挂了——可以试试 get_targets）"

        lines = [f"表达式：{expr}", f"命中 {len(result)} 条序列，显示前 {min(len(result), MAX_SERIES)} 条："]
        for m in result[:MAX_SERIES]:
            label = m["metric"]
            # 只保留最有辨识度的标签，别把整个 label set 倒出来
            ident = " ".join(
                f"{k}={v}" for k, v in sorted(label.items())
                if k in ("namespace", "pod", "deployment", "statefulset",
                         "node", "container", "phase", "job", "instance", "service")
            ) or "(无标签)"
            val = m["value"][1] if "value" in m else str(m.get("values", [["", ""]])[-1][1])
            lines.append(f"  {ident} = {val}")
        if len(result) > MAX_SERIES:
            lines.append(f"  …… 另有 {len(result) - MAX_SERIES} 条未显示")
        return "\n".join(lines)

    def _get_alerts(self, _params: dict[str, Any]) -> str:
        d = self._get_json("api/v1/alerts")
        alerts = d["data"]["alerts"]
        firing = [a for a in alerts if a.get("state") == "firing"]
        pending = [a for a in alerts if a.get("state") == "pending"]
        if not alerts:
            return "当前没有配置或触发任何告警。"
        lines = [f"firing {len(firing)} 条，pending {len(pending)} 条："]
        for a in (firing + pending)[:MAX_SERIES]:
            lbl = a.get("labels", {})
            lines.append(f"  [{a.get('state')}] {lbl.get('alertname')} "
                         f"ns={lbl.get('namespace', '-')} "
                         f"severity={lbl.get('severity', '-')} "
                         f"since={a.get('activeAt', '')[:19]}")
            if a.get("annotations", {}).get("summary"):
                lines.append(f"        {a['annotations']['summary'][:120]}")
        return "\n".join(lines)

    def _get_targets(self, _params: dict[str, Any]) -> str:
        d = self._get_json("api/v1/targets")
        active = d["data"]["activeTargets"]
        down = [t for t in active if t.get("health") != "up"]
        lines = [f"抓取目标共 {len(active)} 个，其中不健康 {len(down)} 个："]
        for t in active[:MAX_SERIES]:
            lines.append(f"  [{t.get('health')}] job={t['labels'].get('job')} "
                         f"{t.get('scrapeUrl', '')[:70]}")
            if t.get("lastError"):
                lines.append(f"        错误：{t['lastError'][:120]}")
        return "\n".join(lines)


class LokiProvider(HttpProvider):
    """Loki：日志取证。

    补的是 K8s 原生 ``get_logs`` 的两个盲区：
    **上一个崩溃实例的日志**，以及**跨 Pod 聚合**（"这 5 个副本是不是都在报同一句错"）。

    它的返回值同样是不可信输入——日志是最容易被塞提示注入的地方，
    所以它和其它工具一样会走 safety 围栏，这一点由 session 层统一保证。
    """

    name = "loki"

    def __init__(self, k8s: K8sClient, *, namespace: str = "monitoring",
                 service: str = "loki", port: int = 3100, url: str = ""):
        super().__init__(k8s, namespace=namespace, service=service,
                         port=port, url=url)

    def available(self) -> tuple[bool, str]:
        try:
            d = self._get_json("loki/api/v1/labels")
        except ProviderError as exc:
            return False, str(exc)[:200]
        labels = d.get("data") or []
        return True, f"Loki（{len(labels)} 个标签）"

    def tools(self) -> list[ProviderTool]:
        return [
            ProviderTool(
                ToolSpec(
                    "query_logs", False,
                    "用 LogQL 查历史/聚合日志。相比 get_logs，它能查**上一个崩溃实例**"
                    "和**跨 Pod 聚合**的日志——排查 CrashLoopBackOff 时关键。"
                    "例：{namespace=\"demo\", app=\"api-gateway\"}，"
                    "或带过滤 {namespace=\"demo\"} |= \"ERROR\"",
                    {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "LogQL 选择器，如 {namespace=\"demo\", app=\"web\"}"},
                            "minutes": {"type": "integer",
                                        "description": "往回查多少分钟，默认 15"},
                            "limit": {"type": "integer",
                                      "description": f"最多返回多少行，默认 {MAX_LOG_LINES}"},
                        },
                        "required": ["query"],
                    },
                ),
                self._query_logs,
            ),
        ]

    def _query_logs(self, params: dict[str, Any]) -> str:
        query = str(params.get("query", "")).strip()
        if not query:
            raise ProviderError("query 不能为空")
        try:
            minutes = int(params.get("minutes") or 15)
        except (TypeError, ValueError):
            minutes = 15
        try:
            limit = min(int(params.get("limit") or MAX_LOG_LINES), MAX_LOG_LINES)
        except (TypeError, ValueError):
            limit = MAX_LOG_LINES

        now = time.time()
        d = self._get_json("loki/api/v1/query_range", {
            "query": query,
            "start": str(int((now - minutes * 60) * 1e9)),
            "end": str(int(now * 1e9)),
            "limit": str(limit),
            "direction": "backward",
        })
        if d.get("status") != "success":
            raise ProviderError(f"LogQL 执行失败：{d.get('error', d)}")

        streams = d["data"]["result"]
        if not streams:
            return f"最近 {minutes} 分钟没有匹配的日志。查询：{query}"

        rows: list[tuple[str, str]] = []
        for s in streams:
            labels = s.get("stream", {})
            tag = " ".join(f"{k}={v}" for k, v in sorted(labels.items())
                           if k in ("pod", "app", "container", "namespace"))
            for _ts, line in s.get("values", []):
                rows.append((tag, line))
        rows.sort(key=lambda r: r[0])

        lines = [f"最近 {minutes} 分钟命中 {len(rows)} 行（显示前 {min(len(rows), limit)} 行）："]
        for tag, line in rows[:limit]:
            lines.append(f"  [{tag}] {line[:400]}")
        if len(rows) > limit:
            lines.append(f"  …… 另有 {len(rows) - limit} 行未显示")
        return "\n".join(lines)
