"""巡检：从"人来问"变成"主动发现"。

但**主动巡检 ≠ 自主执行**——它只触发只读调查，产出仍然是待批卡片。
这里测的是"发现"和"去重"这两件确定性的事。
"""

from __future__ import annotations

from omagent.patrol import Finding, Patrol


class _FakeK8s:
    def __init__(self, bad: dict[str, list[str]] | None = None, down: bool = False):
        self.bad = bad or {}
        self.down = down


def _patrol(monkeypatch, bad, *, cooldown=1800.0, namespaces=("demo",)):
    """把 console.overview 换掉——巡检只依赖它的 healthy/problems。"""
    from omagent import console as console_mod

    def fake_overview(k8s, ns, cases=None, nodes_cache=None):
        rows = [{"kind": "deployment", "name": n, "healthy": False, "problems": p}
                for n, p in (bad.get(ns) or {}).items()]
        rows += [{"kind": "deployment", "name": "ok-svc", "healthy": True, "problems": []}]
        return {"ok": True, "workloads": rows}

    monkeypatch.setattr(console_mod, "overview", fake_overview)
    return Patrol(_FakeK8s(), list(namespaces), cooldown=cooldown)


# --- 发现 -----------------------------------------------------------------


def test_只报不健康的工作负载(monkeypatch):
    p = _patrol(monkeypatch, {"demo": {"api-gateway": ["就绪 2/3", "有 Pod 未就绪"]}})
    found = p.scan_once()
    assert [f.name for f in found] == ["api-gateway"]
    assert found[0].namespace == "demo"
    assert "就绪 2/3" in found[0].problems
    # 健康的不该被报出来
    assert "ok-svc" not in [f.name for f in found]


def test_问题描述会带进给模型的问题里(monkeypatch):
    p = _patrol(monkeypatch, {"demo": {"cart-service": ["反复重启", "退出码 137"]}})
    q = p.scan_once()[0].question()
    assert "cart-service" in q
    assert "反复重启" in q and "退出码 137" in q
    # 已经算出来的现象要给它，别让它重查一遍
    assert "巡检时发现异常" in q


def test_命名空间读不到不影响其它命名空间(monkeypatch):
    """一个命名空间挂了不该让整轮巡检失败——巡检更不能自己变成故障源。"""
    from omagent import console as console_mod

    def flaky(k8s, ns, cases=None, nodes_cache=None):
        if ns == "bad-ns":
            raise RuntimeError("连不上")
        return {"ok": True, "workloads": [
            {"kind": "deployment", "name": "svc", "healthy": False, "problems": ["x"]}]}

    monkeypatch.setattr(console_mod, "overview", flaky)
    p = Patrol(_FakeK8s(), ["bad-ns", "demo"])
    found = p.scan_once()
    assert [f.namespace for f in found] == ["demo"]


# --- 去重（这条是必需的，不是优化）-----------------------------------------


def test_同一个故障在冷却期内不重复报(monkeypatch):
    """一个坏负载会**每次巡检都异常**。不去重的话一天报几百次同样的东西，
    模型成本爆炸、控制台被刷屏，人反而看不见新问题。"""
    p = _patrol(monkeypatch, {"demo": {"api-gateway": ["坏"]}}, cooldown=1800)
    assert len(p.tick()) == 1
    assert p.tick() == []          # 还在冷却期内
    assert p.tick() == []
    assert p.suppressed == 2


def test_冷却期过了会再报(monkeypatch):
    p = _patrol(monkeypatch, {"demo": {"api-gateway": ["坏"]}}, cooldown=100)
    assert len(p.new_findings(p.scan_once(), now=1000.0)) == 1
    assert p.new_findings(p.scan_once(), now=1050.0) == []      # 期内
    assert len(p.new_findings(p.scan_once(), now=1200.0)) == 1  # 过了


def test_只要还坏着不会一直不报(monkeypatch):
    """时间戳只在**过滤之后**更新。

    如果每轮都刷新时间戳，冷却期永远走不完，就变成"只要还坏着就一直不报"——
    那是另一个极端，等于把持续故障彻底静音。
    """
    p = _patrol(monkeypatch, {"demo": {"x": ["坏"]}}, cooldown=100)
    p.new_findings(p.scan_once(), now=1000.0)
    for t in (1010.0, 1020.0, 1030.0):      # 冷却期内的几轮
        p.new_findings(p.scan_once(), now=t)
    assert p.new_findings(p.scan_once(), now=1200.0), "过了冷却期必须重新报"


def test_不同工作负载互不影响冷却(monkeypatch):
    p = _patrol(monkeypatch, {"demo": {"a": ["坏"], "b": ["坏"]}}, cooldown=1800)
    assert sorted(f.name for f in p.tick()) == ["a", "b"]
    assert p.tick() == []


# --- 告警接入 -------------------------------------------------------------


def test_接_alertmanager_的告警(monkeypatch):
    p = _patrol(monkeypatch, {})
    fresh = p.ingest_alerts({"alerts": [
        {"labels": {"namespace": "demo", "deployment": "api-gateway", "alertname": "KubePodCrashLooping"},
         "annotations": {"summary": "容器反复重启"}}]})
    assert len(fresh) == 1
    assert fresh[0].name == "api-gateway"
    assert "容器反复重启" in fresh[0].problems


def test_告警和巡检走同一条去重(monkeypatch):
    """否则同一个故障会被人看到两张卡——一张来自巡检，一张来自告警。"""
    p = _patrol(monkeypatch, {"demo": {"api-gateway": ["坏"]}}, cooldown=1800)
    assert len(p.tick()) == 1                                  # 巡检先报
    again = p.ingest_alerts({"alerts": [
        {"labels": {"namespace": "demo", "deployment": "api-gateway"}}]})
    assert again == [], "告警又报了一次——两条路径没共用去重"


def test_缺命名空间或名字的告警被忽略(monkeypatch):
    p = _patrol(monkeypatch, {})
    assert p.ingest_alerts({"alerts": [{"labels": {"alertname": "X"}}]}) == []


def test_支持手工触发的简单形态(monkeypatch):
    p = _patrol(monkeypatch, {})
    fresh = p.ingest_alerts({"namespace": "demo", "workload": "order-service",
                             "reason": "手工触发看看"})
    assert len(fresh) == 1 and fresh[0].name == "order-service"


# --- 回调 -----------------------------------------------------------------


def test_发现会回调但回调抛异常不打断巡检(monkeypatch):
    """单个调查起不来（模型限流、会话满了）不该让整轮巡检停掉。"""
    p = _patrol(monkeypatch, {"demo": {"a": ["坏"], "b": ["坏"]}})
    seen = []

    def boom(f):
        seen.append(f.name)
        raise RuntimeError("起不来")

    p.on_finding = boom
    assert sorted(f.name for f in p.tick()) == ["a", "b"]   # 两个都被处理了
    assert sorted(seen) == ["a", "b"]


def test_status_给人看(monkeypatch):
    p = _patrol(monkeypatch, {"demo": {"a": ["坏"]}}, cooldown=600)
    p.tick()
    st = p.status()
    assert st["scans"] == 1 and st["findings_total"] == 1
    assert st["cooldown_seconds"] == 600
    assert st["watching"] == ["demo/deployment/a"]
    assert st["enabled"] is False      # 没 start 就不该说开着
