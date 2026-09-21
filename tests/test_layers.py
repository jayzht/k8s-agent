"""各根因层的测试：服务层 / 节点层 / 配额 / 依赖归属。

对应 PRD 4.1 的「症状 × 根因层」能力矩阵。每一层都配了**反向对照**，
因为"多报一层"和"漏报一层"一样糟——会把运维引向错误的排查方向。
"""

from __future__ import annotations

import pytest

from omagent.evals import EvalCase, run_case


def _run(**fx_over):
    fx = {
        "workload": {"kind": "Deployment", "name": "api-gateway", "replicas": 2,
                     "labels": {"app": "api-gateway"}},
        "container_ports": [8080],
        "services": ["api-gateway"],
        "service_ports": {"api-gateway": [80]},
        "endpoints": {"api-gateway": 2},
        "pods": [{"name": "p1", "phase": "Running", "ready": True, "restarts": 0}],
    }
    fx.update(fx_over)
    return run_case(EvalCase.from_dict({
        "id": "layer", "title": "层测试", "fixture": fx,
        "expect": {"signature": "", "acceptable_actions": [],
                   "forbidden_actions": ["delete_workload"]},
    }))


# ---------------------------------------------------------------------------
# 服务层：targetPort
# ---------------------------------------------------------------------------


def test_target_port_mismatch_detected():
    """Pod 正常、Endpoints 非空，但 targetPort 转发到了没人监听的端口。"""
    r = _run(target_ports={"api-gateway": 9999})
    assert r.signature_actual == "service_target_port_mismatch"
    assert not r.mutating_proposals, "根因在 Service 定义，不该给 Pod 侧动作"


def test_target_port_matching_does_not_fire():
    r = _run(target_ports={"api-gateway": 8080})
    assert r.signature_actual != "service_target_port_mismatch"


def test_target_port_defaults_to_container_port():
    """未显式指定 targetPort 时默认与容器端口对齐，不应误报。"""
    r = _run()
    assert r.signature_actual != "service_target_port_mismatch"


def test_target_port_mismatch_mentions_both_ports():
    r = _run(target_ports={"api-gateway": 9999})
    assert "9999" in r.conclusion and "8080" in r.conclusion


# ---------------------------------------------------------------------------
# 节点/集群层：压力
# ---------------------------------------------------------------------------


def test_node_pressure_detected():
    r = _run(
        node_pressure={"worker-1": ["DiskPressure"]},
        pods=[{"name": "p1", "phase": "Failed", "ready": False, "restarts": 0,
               "node": "worker-1", "reason": "Evicted"}],
    )
    assert r.signature_actual == "node_pressure"
    assert not r.mutating_proposals, "集群级问题不该对本工作负载动手"


def test_no_node_pressure_does_not_fire():
    r = _run(node_pressure={"worker-1": []})
    assert r.signature_actual != "node_pressure"


def test_memory_pressure_also_detected():
    r = _run(
        node_pressure={"worker-1": ["MemoryPressure"]},
        pods=[{"name": "p1", "phase": "Running", "ready": False, "restarts": 2,
               "node": "worker-1"}],
    )
    assert r.signature_actual == "node_pressure"


def test_node_pressure_conclusion_names_the_node():
    r = _run(node_pressure={"worker-1": ["DiskPressure"]},
             pods=[{"name": "p1", "phase": "Failed", "ready": False, "restarts": 0,
                    "node": "worker-1"}])
    assert "worker-1" in r.conclusion and "DiskPressure" in r.conclusion


# ---------------------------------------------------------------------------
# 节点/集群层：配额
# ---------------------------------------------------------------------------


def test_quota_exhausted_beats_no_pods():
    """★ 配额耗尽必须优先于"没有 Pod"——两者处置完全不同。"""
    r = _run(pods=[], quotas=[{"name": "q", "hard": {"pods": "20"}, "used": {"pods": "20"}}])
    assert r.signature_actual == "quota_exhausted"
    assert "no_pods" != r.signature_actual


def test_quota_with_headroom_falls_through_to_no_pods():
    r = _run(pods=[], quotas=[{"name": "q", "hard": {"pods": "20"}, "used": {"pods": "5"}}])
    assert r.signature_actual == "no_pods"


def test_no_quota_at_all_is_not_quota_exhausted():
    r = _run(pods=[])
    assert r.signature_actual == "no_pods"


# ---------------------------------------------------------------------------
# 依赖层：三分归属
# ---------------------------------------------------------------------------


def _dep(**fx_over):
    fx = {
        "workload": {"kind": "Deployment", "name": "checkout", "replicas": 1,
                     "labels": {"app": "checkout"}},
        "env": {"KAFKA_ADDR": "kafka:9092"},
        "service_ports": {"kafka": [9092]},
        "endpoints": {"kafka": 0, "checkout": 0},
        "previous_logs": "ERROR failed to connect to kafka:9092: connection refused\n",
        "pods": [{"name": "c1", "phase": "Running", "ready": False, "restarts": 4,
                  "last_exit_code": 1, "reason": "CrashLoopBackOff"}],
        "services": ["checkout"],
    }
    fx.update(fx_over)
    return run_case(EvalCase.from_dict({
        "id": "dep", "title": "依赖层", "fixture": fx,
        "expect": {"signature": "", "acceptable_actions": [],
                   "forbidden_actions": ["delete_workload"]},
    }))


def test_dependency_down_is_attributed_to_dependency():
    """★ 端口对得上、对方没后端 → 是对方挂了，本工作负载别动手。"""
    r = _dep()
    assert r.signature_actual == "dependency_unavailable"
    assert not r.mutating_proposals


def test_healthy_dependency_becomes_network_issue():
    r = _dep(endpoints={"kafka": 2, "checkout": 1})
    assert r.signature_actual == "network_blocked"
    assert not r.mutating_proposals


def test_port_mismatch_takes_priority_over_dependency():
    """★ 归因优先级：端口冲突 > 依赖无后端 > 网络。

    否则"自己配错了"会被误判成"对方挂了"，方向完全相反。
    """
    r = _dep(env={"KAFKA_ADDR": "kafka:9999"},
             previous_logs="ERROR failed to connect to kafka:9999: connection refused\n")
    assert r.signature_actual == "config_misconfiguration"


def test_network_policies_are_surfaced():
    r = _dep(endpoints={"kafka": 2, "checkout": 1}, networkpolicies=["deny-egress"])
    assert r.signature_actual == "network_blocked"
    assert "deny-egress" in r.conclusion


def test_unrelated_host_not_attributed():
    """日志里的地址不是本命名空间的 Service（如集群外地址）时不做归属判定。"""
    r = _dep(env={"API": "api.vendor.com:443"},
             previous_logs="ERROR failed to connect to api.vendor.com:443: i/o timeout\n")
    assert r.signature_actual not in (
        "dependency_unavailable", "network_blocked", "config_misconfiguration"
    )


# ---------------------------------------------------------------------------
# 流量入口：Ingress 后端
# ---------------------------------------------------------------------------


def test_ingress_backend_missing_service_detected():
    r = _run(ingresses=[{"name": "ing", "backends": [{"service": "typo", "port": 80}]}])
    assert r.signature_actual == "ingress_backend_invalid"
    assert not r.mutating_proposals, "根因在 Ingress，Pod 侧动作无效"


def test_ingress_backend_wrong_port_detected():
    r = _run(ingresses=[{"name": "ing",
                         "backends": [{"service": "api-gateway", "port": 9999}]}])
    assert r.signature_actual == "ingress_backend_invalid"


def test_ingress_backend_valid_does_not_fire():
    r = _run(ingresses=[{"name": "ing",
                         "backends": [{"service": "api-gateway", "port": 80}]}])
    assert r.signature_actual == "healthy"


# ---------------------------------------------------------------------------
# 命名空间策略：LimitRange
# ---------------------------------------------------------------------------


def test_limitrange_violation_detected():
    r = _run(
        limitranges=[{"name": "lr", "limits": [{"type": "Container",
                                                "max": {"memory": "512Mi"}}]}],
        memory_requests={"memory": "1Gi"},
        pods=[],
    )
    assert r.signature_actual == "limitrange_violation"


def test_limitrange_within_bounds_does_not_fire():
    r = _run(
        limitranges=[{"name": "lr", "limits": [{"type": "Container",
                                                "max": {"memory": "1Gi"}}]}],
        memory_requests={"memory": "128Mi"},
        pods=[],
    )
    assert r.signature_actual == "no_pods"


def test_limitrange_survives_unparseable_quantity():
    """无法解析的数量（如自定义单位）不能导致误报。"""
    r = _run(
        limitranges=[{"name": "lr", "limits": [{"type": "Container",
                                                "max": {"memory": "1Gi"}}]}],
        memory_requests={"memory": ""},
        pods=[],
    )
    assert r.signature_actual != "limitrange_violation"


# ---------------------------------------------------------------------------
# 配置层写动作：ConfigMap 版本回滚
#
# K8s 原生不保留 ConfigMap 历史，版本由本项目的 ConfigMapHistory 记录——
# 这正是"配置层能回滚"的前提。这里验证整条链路真的通。
# ---------------------------------------------------------------------------


def test_configmap_rollback_offered_when_history_exists(tmp_path, monkeypatch):
    from omagent.knowledge import ConfigMapHistory

    monkeypatch.setenv("OMAGENT_CM_HISTORY", str(tmp_path / "cm.json"))
    hist = ConfigMapHistory()
    hist.record("demo", "app-config", {"KAFKA_ADDR": "kafka:9092"})   # 正确版本
    hist.record("demo", "app-config", {"KAFKA_ADDR": "kafka:9999"})   # 改坏的版本

    r = _run(
        workload={"kind": "Deployment", "name": "checkout", "replicas": 1,
                  "labels": {"app": "checkout"}},
        env={"KAFKA_ADDR": "kafka:9999"},
        service_ports={"kafka": [9092]},
        configmaps=["app-config"],
        previous_logs="ERROR failed to connect to kafka:9999: connection refused\n",
        pods=[{"name": "c1", "phase": "Running", "ready": False, "restarts": 3,
               "last_exit_code": 1, "reason": "CrashLoopBackOff"}],
        services=["checkout"],
        endpoints={"kafka": 1, "checkout": 0},
    )
    assert r.signature_actual == "config_misconfiguration"
    assert "rollback_configmap" in r.mutating_proposals, "有历史版本时应给出配置回滚"


def test_no_configmap_history_no_rollback_offered(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAGENT_CM_HISTORY", str(tmp_path / "empty.json"))
    r = _run(
        workload={"kind": "Deployment", "name": "checkout", "replicas": 1,
                  "labels": {"app": "checkout"}},
        env={"KAFKA_ADDR": "kafka:9999"},
        service_ports={"kafka": [9092]},
        configmaps=["app-config"],
        previous_logs="ERROR failed to connect to kafka:9999: connection refused\n",
        pods=[{"name": "c1", "phase": "Running", "ready": False, "restarts": 3,
               "last_exit_code": 1, "reason": "CrashLoopBackOff"}],
        services=["checkout"],
        endpoints={"kafka": 1, "checkout": 0},
    )
    assert "rollback_configmap" not in r.mutating_proposals
    assert "rollout_undo" in r.mutating_proposals, "没有历史版本时仍应给出工作负载回滚"


def test_rollback_configmap_is_registered_as_t2_mutating():
    from omagent.agent import TOOLS
    from omagent.policy import Policy

    rule = Policy().actions["rollback_configmap"]
    assert rule.tier.value == "T2" and rule.mutating
    assert TOOLS["rollback_configmap"].mutating is True
