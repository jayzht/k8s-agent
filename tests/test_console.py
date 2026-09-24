"""监控台的数据组装测试。

这个文件是补上的。在此之前 `console.py` **一条测试都没有**，而它恰好是
真 bug 的重灾区——今天发现的三个全都出在这里：

  1. DaemonSet 没有 ``spec.replicas``，一刀切去读它会抛 AttributeError，
     整个命名空间读不出来；
  2. HPA 的 conditions 只有 **v2** API 才有，走 v1 读到的对象上没这个属性，
     异常被外层 except 吞掉，"HPA 失效了"这个信号静默消失；
  3. desired==0 的工作负载（比如被缩到 0 的主库）看起来"没有异常"。

三条现在都有测试守着。
"""

from __future__ import annotations

import pytest

from omagent import console

from .fakes import FakeConsoleK8s, container, hpa, pod, workload


def row_of(data, name):
    return next((w for w in data["workloads"] if w["name"] == name), None)


# ── DaemonSet ─────────────────────────────────────────────────────────────


def test_daemonset_的期望副本数来自_status_而不是_spec():
    """DaemonSet 没有 spec.replicas。早期版本一刀切读它，直接抛 AttributeError，
    整个命名空间都读不出来（不是少一行，是整块报错）。"""
    k8s = FakeConsoleK8s(
        pods=[pod("lc-1", labels={"app": "log-collector"}),
              pod("lc-2", labels={"app": "log-collector"})],
        workloads=[workload("log-collector", "daemonset", replicas=2,
                            labels={"app": "log-collector"})],
    )
    data = console.overview(k8s, "observability")
    assert data["ok"] is True
    row = row_of(data, "log-collector")
    assert row is not None, "DaemonSet 必须出现在监控台里"
    assert row["desired"] == 2
    assert row["ready"] == 2
    assert row["healthy"] is True


def test_daemonset_副本不足会被标红():
    k8s = FakeConsoleK8s(
        pods=[pod("lc-1", labels={"app": "log-collector"})],
        workloads=[workload("log-collector", "daemonset", replicas=3, ready=1,
                            labels={"app": "log-collector"})],
    )
    data = console.overview(k8s, "observability")
    row = row_of(data, "log-collector")
    assert row["desired"] == 3
    assert row["ready"] == 1
    assert row["healthy"] is False
    assert any("就绪" in p for p in row["problems"])


# ── 缩容到 0 ──────────────────────────────────────────────────────────────


def test_缩容到0会被标出来():
    """主库被缩到 0 时，0/0 看起来像"没事"。这是"依赖被关掉"的唯一可见信号。"""
    k8s = FakeConsoleK8s(
        pods=[],
        workloads=[workload("postgres-primary", "statefulset", replicas=0,
                            labels={"app": "postgres-primary"})],
    )
    data = console.overview(k8s, "demo")
    row = row_of(data, "postgres-primary")
    assert row["healthy"] is False
    assert any("缩容到 0" in p for p in row["problems"])


# ── Service / Endpoints ───────────────────────────────────────────────────


def test_服务没有后端会被标出来():
    """selector 写错时 Pod 全是好的——只看 Pod 状态的监控台对这类故障完全瞎。"""
    k8s = FakeConsoleK8s(
        pods=[pod("o-1", labels={"app": "order-service"})],
        workloads=[workload("order-service", replicas=1,
                            labels={"app": "order-service"})],
        services=[{"name": "order-service", "selector": {"app": "order-service-v2"},
                   "ports": [{"port": 80, "target_port": "8080"}]}],
        endpoints={"order-service": {"ready": 0, "not_ready": 0}},
    )
    data = console.overview(k8s, "demo")
    # selector 改坏之后谁也匹配不上，所以只能进"独立异常"
    assert data["summary"]["standalone"] == 1
    assert any("没有任何后端" in p for p in data["standalone"][0]["problems"])


def test_targetPort_与容器端口对不上会被标出来():
    """端口对不上时 Endpoints 看起来是正常的（有地址），但连接会被拒绝。"""
    k8s = FakeConsoleK8s(
        pods=[pod("p-1", labels={"app": "payment-service"})],
        workloads=[workload("payment-service", replicas=1,
                            labels={"app": "payment-service"},
                            containers=[container(ports=(8080,))])],
        services=[{"name": "payment-service", "selector": {"app": "payment-service"},
                   "ports": [{"port": 80, "target_port": "9999"}]}],
        endpoints={"payment-service": {"ready": 1, "not_ready": 0}},
    )
    data = console.overview(k8s, "demo")
    row = row_of(data, "payment-service")
    assert row["healthy"] is False
    assert any("targetPort 9999" in p for p in row["problems"])


def test_targetPort_正常时不报错():
    k8s = FakeConsoleK8s(
        pods=[pod("p-1", labels={"app": "payment-service"})],
        workloads=[workload("payment-service", replicas=1,
                            labels={"app": "payment-service"},
                            containers=[container(ports=(8080,))])],
        services=[{"name": "payment-service", "selector": {"app": "payment-service"},
                   "ports": [{"port": 80, "target_port": "8080"}]}],
        endpoints={"payment-service": {"ready": 1, "not_ready": 0}},
    )
    data = console.overview(k8s, "demo")
    assert row_of(data, "payment-service")["healthy"] is True


# ── HPA ───────────────────────────────────────────────────────────────────


def test_hpa_扩缩容目标坏了会被标出来():
    """AbleToScale=False（FailedGetScale）才是"扩缩容目标坏了"。"""
    k8s = FakeConsoleK8s(
        pods=[pod("a-1", labels={"app": "api-gateway"})],
        workloads=[workload("api-gateway", replicas=1, labels={"app": "api-gateway"})],
        hpas=[hpa("api-gateway", "api-gateway-typo",
                  [("AbleToScale", "False", "FailedGetScale"),
                   ("ScalingActive", "False", "FailedGetResourceMetric")])],
    )
    data = console.overview(k8s, "demo")
    row = row_of(data, "api-gateway")
    assert row["healthy"] is False
    assert any("FailedGetScale" in p for p in row["problems"])


def test_只有_ScalingActive_为_False_不算异常():
    """这是关键：沙箱（以及很多没装 metrics-server 的集群）里
    ScalingActive 会**永远是 False**。拿它当判据的话，
    每个挂了 HPA 的负载都会常年标红，信号直接废掉。"""
    k8s = FakeConsoleK8s(
        pods=[pod("a-1", labels={"app": "api-gateway"})],
        workloads=[workload("api-gateway", replicas=1, labels={"app": "api-gateway"})],
        hpas=[hpa("api-gateway", "api-gateway",
                  [("AbleToScale", "True", "ReadyForNewScale"),
                   ("ScalingActive", "False", "FailedGetResourceMetric")])],
    )
    data = console.overview(k8s, "demo")
    assert row_of(data, "api-gateway")["healthy"] is True
    assert data["summary"]["standalone"] == 0


# ── 能力标记（给故障菜单用的）────────────────────────────────────────────


def test_能力标记反映目标能演哪些故障():
    k8s = FakeConsoleK8s(
        pods=[pod("o-1", labels={"app": "order-service"}),
              pod("c-1", labels={"app": "cart-service"})],
        workloads=[
            workload("order-service", replicas=1, labels={"app": "order-service"},
                     containers=[container(configmaps=("order-service-config",))]),
            workload("cart-service", replicas=1, labels={"app": "cart-service"}),
        ],
        services=[{"name": "cart-service", "selector": {"app": "cart-service"},
                   "ports": [{"port": 80, "target_port": "8080"}]}],
        endpoints={"cart-service": {"ready": 1, "not_ready": 0}},
        hpas=[hpa("order-service", "order-service",
                  [("AbleToScale", "True", "ReadyForNewScale")])],
    )
    data = console.overview(k8s, "demo")

    order = row_of(data, "order-service")
    assert order["has_configmap"] is True
    assert order["has_hpa"] is True

    cart = row_of(data, "cart-service")
    assert cart["has_configmap"] is False
    assert cart["has_hpa"] is False
    assert cart["has_service"] is True
    assert cart["has_node_selector"] is False


def test_节点池选择器会被读出来():
    k8s = FakeConsoleK8s(
        pods=[pod("r-1", labels={"app": "reporting-worker"})],
        workloads=[workload("reporting-worker", replicas=1,
                            labels={"app": "reporting-worker"}, node_pool="batch")],
    )
    data = console.overview(k8s, "demo")
    assert row_of(data, "reporting-worker")["has_node_selector"] is True


# ── 排序与汇总 ────────────────────────────────────────────────────────────


def test_异常负载排在最前面():
    k8s = FakeConsoleK8s(
        pods=[pod("a-1", labels={"app": "aaa-healthy"}),
              pod("z-1", phase="Pending", ready=False, labels={"app": "zzz-broken"})],
        workloads=[workload("aaa-healthy", replicas=1, labels={"app": "aaa-healthy"}),
                   workload("zzz-broken", replicas=1, labels={"app": "zzz-broken"})],
    )
    data = console.overview(k8s, "demo")
    assert data["workloads"][0]["name"] == "zzz-broken"
    assert data["summary"]["unhealthy"] == 1
