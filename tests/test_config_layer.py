"""配置层诊断的测试。

背景：ITBench-Lite 真实数据里，配置类根因占 12/35，是占比最大的一类。
而早期实现只读 Pod 状态——**Pod 健康就报 healthy，业务全红却看不见**。
这一层补的就是这个缺口。
"""

from __future__ import annotations

import pytest

from omagent.evals import EvalCase, run_case
from omagent.planner import extract_connect_signals


def _case(**fx_over) -> dict:
    fx = {
        "workload": {"kind": "Deployment", "name": "checkout", "replicas": 1,
                     "labels": {"app": "checkout"}},
        "env": {"KAFKA_ADDR": "kafka:9999"},
        "service_ports": {"kafka": [9092]},
        "pods": [{"name": "checkout-1", "phase": "Running", "ready": False,
                  "restarts": 3, "last_exit_code": 1, "reason": "CrashLoopBackOff"}],
        "services": ["checkout"],
        "previous_logs": "ERROR failed to connect to kafka:9999: connection refused\n",
    }
    fx.update(fx_over)
    return {
        "id": "cfg", "title": "配置层用例",
        "fixture": fx,
        "expect": {"signature": "config_misconfiguration",
                   "acceptable_actions": ["rollout_undo"],
                   "forbidden_actions": ["rollout_restart", "delete_workload"]},
    }


# ---------------------------------------------------------------------------
# 信号提取
# ---------------------------------------------------------------------------


def test_extract_signals_from_various_wording():
    """多种措辞都要能提取出 host:port。

    注意：DNS 失败（`no such host`）这类**不带端口**的信号提取不到——
    而配置层比对恰恰需要端口，所以这是已知且可接受的局限。
    """
    logs = (
        "ERROR failed to connect to kafka:9999: connection refused\n"
        "dial tcp 10.96.0.5:9092: connect: connection refused\n"
        "ERROR Get \"http://quote:8080/health\": dial tcp quote:8080: i/o timeout\n"
    )
    addrs = {s.addr for s in extract_connect_signals(logs)}
    assert "kafka:9999" in addrs
    assert "10.96.0.5:9092" in addrs
    assert "quote:8080" in addrs


def test_dns_failure_without_port_yields_no_signal():
    """无端口的 DNS 失败提取不到信号——记录该局限，避免误以为覆盖了。"""
    assert extract_connect_signals("ERROR lookup redis.svc: no such host\n") == []


def test_extract_signals_ignores_clean_logs():
    assert extract_connect_signals("INFO server listening on :8080\n") == []
    assert extract_connect_signals("") == []
    assert extract_connect_signals(None) == []


def test_signal_preserves_raw_port_text():
    """'0000' 不能被 int() 显示成 '0'——真实数据里出现过这个形态。"""
    sigs = extract_connect_signals("dial tcp quote:0000: connection refused")
    assert sigs and sigs[0].addr == "quote:0000"


# ---------------------------------------------------------------------------
# 检测：日志 + 配置 + 真实端口 三方比对
# ---------------------------------------------------------------------------


def test_config_mismatch_detected_with_logs():
    r = run_case(EvalCase.from_dict(_case()))
    assert r.signature_actual == "config_misconfiguration"
    assert "rollout_undo" in r.mutating_proposals
    assert "rollout_restart" not in r.mutating_proposals


def test_config_mismatch_mentions_the_env_var():
    r = run_case(EvalCase.from_dict(_case()))
    assert "KAFKA_ADDR" in r.conclusion


def test_matching_port_does_not_fire():
    """★ 反向：端口对得上说明配置没错，是依赖真的挂了——不该归咎于本工作负载。"""
    raw = _case(env={"KAFKA_ADDR": "kafka:9092"},
                previous_logs="ERROR failed to connect to kafka:9092: connection refused\n")
    raw["expect"]["signature"] = "crashloop"
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual != "config_misconfiguration"


def test_unknown_host_does_not_fire():
    """env 指向集群外地址（不是本命名空间的 Service）时不得判为配置错误。"""
    raw = _case(env={"EXTERNAL_API": "api.vendor.com:443"},
                service_ports={"kafka": [9092]},
                previous_logs="ERROR failed to connect to api.vendor.com:443: timeout\n")
    raw["expect"]["signature"] = "crashloop"
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual != "config_misconfiguration"


def test_nothing_to_cross_check_does_not_fire():
    """既没有 env、也没有 Service 端口信息时无从比对，不得凭空判定配置错误。"""
    raw = _case(env={}, service_ports={},
                previous_logs="ERROR failed to connect to kafka:9999: connection refused\n")
    raw["expect"]["signature"] = "crashloop"
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual != "config_misconfiguration"


def test_log_port_conflict_without_env_still_fires_but_medium():
    """日志端口与 Service 端口冲突、但拿不到 env 时：

    冲突是客观事实，仍应报出——只是没有 env 佐证，置信度降为 medium，
    且结论里不应声称"某个环境变量正是这个地址"。
    """
    raw = _case(env={}, service_ports={"kafka": [9092]},
                previous_logs="ERROR dial tcp kafka:9999: connection refused\n")
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual == "config_misconfiguration"
    # 没有 env 证据时不得声称"某个环境变量正是这个地址"；
    # 但有日志实证，所以应以"日志显示连不上"起头
    assert "正是这个地址" not in r.conclusion
    assert "日志显示应用连不上" in r.conclusion


# ---------------------------------------------------------------------------
# 最危险的一类漏报：Pod 全绿但业务不可用
# ---------------------------------------------------------------------------


def test_healthy_pods_with_config_conflict_is_detected():
    """★ 2 个 Pod 全部 Ready + 0 重启，但配置指向不存在的端口。

    早期实现会输出"当前健康"——**业务全红却报健康**。这是真实数据里最常见的一类。
    """
    raw = _case(
        pods=[{"name": "shipping-a", "phase": "Running", "ready": True, "restarts": 0},
              {"name": "shipping-b", "phase": "Running", "ready": True, "restarts": 0}],
        env={"QUOTE_ADDR": "quote:8080"},
        service_ports={"quote": [9090]},
        previous_logs="ERROR failed to connect to quote:8080: connection refused\n",
        endpoints={"checkout": 2},
    )
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual == "config_misconfiguration"
    assert "rollout_undo" in r.mutating_proposals


def test_healthy_pods_without_logs_still_detects_port_conflict():
    """日志拿不到时的降级检查：配置里的端口在对应 Service 上不存在。

    这条让配置层不依赖日志采集——只有对象快照也能发现问题。
    """
    raw = _case(
        pods=[{"name": "shipping-a", "phase": "Running", "ready": True, "restarts": 0}],
        env={"QUOTE_ADDR": "quote:8080"},
        service_ports={"quote": [9090]},
        previous_logs="",
        endpoints={"checkout": 1},
    )
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual == "config_misconfiguration"
    assert "quote:8080" in r.conclusion


def test_healthy_and_consistent_config_stays_healthy():
    """反向：健康 + 配置自洽 → 必须仍是 healthy，不能误报。"""
    raw = _case(
        pods=[{"name": "p1", "phase": "Running", "ready": True, "restarts": 0}],
        env={"CACHE_ADDR": "redis:6379"},
        service_ports={"redis": [6379]},
        previous_logs="INFO http server listening on :8080\n",
        endpoints={"checkout": 1},
    )
    raw["expect"]["signature"] = "healthy"
    r = run_case(EvalCase.from_dict(raw))
    assert r.signature_actual == "healthy"


# ---------------------------------------------------------------------------
# 动作正确性
# ---------------------------------------------------------------------------


def test_config_never_recommends_restart():
    """配置错了，重启无效——绝不能建议重启。"""
    for raw in (
        _case(),
        _case(pods=[{"name": "p", "phase": "Running", "ready": True, "restarts": 0}],
              previous_logs="ERROR dial tcp kafka:9999: connection refused\n"),
    ):
        r = run_case(EvalCase.from_dict(raw))
        assert "rollout_restart" not in r.mutating_proposals
        assert "scale_workload" not in r.mutating_proposals
