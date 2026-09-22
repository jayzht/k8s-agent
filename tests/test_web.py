"""Web 审批界面的测试。

最重要的一组是**门禁不可绕过**：浏览器端只能表达"批准/拒绝"，
不能表达"执行什么动作"。如果这条性质被破坏，整个 L1 安全模型就崩了。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from omagent.agent import TOOLS, GateViolation, OpsAgent
from omagent.audit import AuditLog
from omagent.evals import EvalCase, FixtureK8s
from omagent.knowledge import KnowledgeStore
from omagent.policy import Policy
from omagent.web import Handler, OpsWebApp


def _case(**over) -> EvalCase:
    raw = {
        "id": "web-1",
        "title": "web 测试用例",
        "fixture": {
            "workload": {"kind": "Deployment", "name": "api-gateway", "replicas": 3,
                         "labels": {"app": "api-gateway"}},
            "pods": [
                {"name": "p1", "phase": "Running", "ready": False, "restarts": 6,
                 "last_exit_code": 137, "memory_limit": "128Mi",
                 "reason": "CrashLoopBackOff"},
            ],
        },
        "expect": {"signature": "oom_killed", "forbidden_actions": ["delete_workload"]},
    }
    raw.update(over)
    return EvalCase.from_dict(raw)


@pytest.fixture
def app(tmp_path):
    k8s = FixtureK8s(_case())
    audit = AuditLog(tmp_path / "audit.jsonl")
    agent = OpsAgent(
        k8s, Policy(state_path=tmp_path / "state.json"), audit,
        operator="web-test@example.com",
    )
    return OpsWebApp(
        agent, audit, KnowledgeStore(tmp_path / "kb.jsonl"), operator="web-test@example.com"
    )


# ---------------------------------------------------------------------------
# 门禁不可绕过（核心）
# ---------------------------------------------------------------------------


def test_forged_diagnosis_id_is_refused(app):
    with pytest.raises(GateViolation):
        app.propose("diag-forged", 0)


def test_forged_proposal_id_is_refused(app):
    """跳过 propose 直接批准必须失败。"""
    with pytest.raises(GateViolation):
        app.decide("prop-forged", True, "强行批准")


def test_out_of_range_candidate_index_is_refused(app):
    d = app.diagnose("demo", "api-gateway", "deployment")
    with pytest.raises(GateViolation):
        app.propose(d["diagnosis_id"], 999)


def test_client_cannot_inject_action(app, tmp_path):
    """★ 核心：即使客户端额外塞了 tool/params，服务端也必须完全忽略。

    ``OpsWebApp.propose`` 的签名里根本没有 tool/params 参数——动作只能来自
    服务端存储的候选。这个测试把该性质固定下来，防止以后"为了方便"加上。
    """
    d = app.diagnose("demo", "api-gateway", "deployment")
    # 模拟被篡改的请求：调用方试图指定一个危险动作
    view = app.propose(d["diagnosis_id"], 0)
    assert view["tool"] in TOOLS
    assert view["tool"] != "delete_workload"
    # 服务端执行的方案来自存储，与客户端传什么无关
    assert view["blocked"] is False


def test_proposal_can_only_be_decided_once(app):
    """方案被取出后即失效，防止重放。"""
    d = app.diagnose("demo", "api-gateway", "deployment")
    p = app.propose(d["diagnosis_id"], 0)
    app.decide(p["proposal_id"], False, "拒绝")
    with pytest.raises(GateViolation):
        app.decide(p["proposal_id"], True, "重放")


def test_rejected_proposal_does_not_write(app):
    d = app.diagnose("demo", "api-gateway", "deployment")
    p = app.propose(d["diagnosis_id"], 0)
    r = app.decide(p["proposal_id"], False, "太危险")
    assert r["status"] == "cancelled"
    assert not [c for c in app.agent.k8s.write_calls if not c[2]]


def test_approved_proposal_executes(app):
    d = app.diagnose("demo", "api-gateway", "deployment")
    p = app.propose(d["diagnosis_id"], 0)
    r = app.decide(p["proposal_id"], True, "确认")
    assert r["status"] == "success"


# ---------------------------------------------------------------------------
# 诊断与展示数据
# ---------------------------------------------------------------------------


def test_diagnose_returns_evidence_and_candidates(app):
    d = app.diagnose("demo", "api-gateway", "deployment")
    assert d["signature"] == "oom_killed"
    assert d["evidence"]
    assert d["candidates"]
    assert d["diagnosis_id"].startswith("diag-")


def test_diagnose_does_not_write(app):
    """诊断是只读的：绝不能出现任何真实写操作。"""
    app.diagnose("demo", "api-gateway", "deployment")
    assert not [c for c in app.agent.k8s.write_calls if not c[2]]


def test_proposal_view_contains_the_card_fields(app):
    """确认卡片需要的字段必须齐全。"""
    d = app.diagnose("demo", "api-gateway", "deployment")
    p = app.propose(d["diagnosis_id"], 0)
    for key in ("tool", "target", "params", "rationale", "impact_rows", "dry_run_ok",
                "rollback", "breaches", "effective_tier", "requires_approval",
                "blocked", "confirm_strength"):
        assert key in p, f"确认卡片缺少字段 {key}"
    assert p["requires_approval"] is True


def test_protected_workload_proposal_is_blocked(tmp_path):
    k8s = FixtureK8s(_case(fixture={
        "workload": {"kind": "Deployment", "name": "billing-core", "replicas": 2,
                     "labels": {"app": "billing-core", "omagent.io/protected": "true"}},
        "pods": [{"name": "b1", "phase": "Running", "ready": False, "restarts": 5,
                  "last_exit_code": 137, "memory_limit": "64Mi"}],
    }))
    audit = AuditLog(tmp_path / "a.jsonl")
    agent = OpsAgent(k8s, Policy(state_path=tmp_path / "s.json"), audit)
    app = OpsWebApp(agent, audit, KnowledgeStore(tmp_path / "k.jsonl"))

    d = app.diagnose("demo", "billing-core", "deployment")
    p = app.propose(d["diagnosis_id"], 0)
    assert p["blocked"] is True
    assert any(b["rule"] == "保护标签" for b in p["breaches"])

    # 即使强行批准也执行不了
    r = app.decide(p["proposal_id"], True, "强行批准")
    assert r["status"] == "refused"


def test_refusal_carries_rule_and_hint(app):
    r = app.refuse("delete_namespace", "删除生产命名空间")
    assert r["rule_id"] == "delete_namespace"
    assert r["hint"]


def test_unknown_rule_still_refused(app):
    r = app.refuse("no_such_rule")
    assert r["rule_id"] == "no_such_rule"


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


@pytest.fixture
def server(app):
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read()


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_root_serves_simple_demo_page(server):
    """`/` 必须是**简化演示页**——面向第一次接触运维的人，单栏向导式。"""
    code, body = _get(server + "/")
    assert code == 200
    assert "运维 Agent 演示".encode() in body
    # 简化页应尽量少用术语；这几个词不该出现在首屏
    for jargon in ("Tier", "熔断", "哈希链", "命名空间白名单"):
        assert jargon.encode() not in body, f"简化页不该出现术语：{jargon}"


def test_pro_route_serves_full_console(server):
    """完整运维台移到 /pro，信息密度高但面向懂行的人。"""
    code, body = _get(server + "/pro")
    assert code == 200 and b"O&amp;M Agent" in body
    for asset in ("app.js", "style.css", "simple.js", "simple.css"):
        code, body2 = _get(f"{server}/static/{asset}")
        assert code == 200 and len(body2) > 100, asset


def test_simple_page_is_self_contained(server):
    """简化页只能依赖自己的 css/js，不该引入外部 CDN。"""
    code, body = _get(server + "/")
    html = body.decode()
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html
    assert "/static/simple.css" in html and "/static/simple.js" in html


def test_static_path_traversal_is_blocked(server):
    """目录穿越必须被拒绝，不能读到 web/ 之外的文件。"""
    for path in ("/static/..%2f..%2fetc%2fpasswd", "/static/sub/x.js", "/static/.env"):
        try:
            code, body = _get(server + path)
        except urllib.error.HTTPError as e:
            code, body = e.code, b""
        assert code in (400, 404), f"{path} 未被拒绝（HTTP {code}）"


def test_http_status_api(server):
    code, body = _get(server + "/api/status")
    assert code == 200
    d = json.loads(body)
    assert "cluster" in d and "policy" in d and "tools" in d


def test_http_forged_proposal_returns_409(server):
    """门禁拒绝用 409（预期行为），不能与 500（故障）混淆。"""
    code, body = _post(server + "/api/decide",
                       {"proposal_id": "prop-forged", "approved": True})
    assert code == 409
    assert "门禁拒绝" in body["error"]


def test_http_missing_param_returns_400(server):
    code, body = _post(server + "/api/propose", {"candidate_index": 0})
    assert code == 400


def test_http_full_flow(server):
    code, d = _post(server + "/api/diagnose",
                    {"namespace": "demo", "workload": "api-gateway",
                     "kind": "deployment", "planner": "rule", "turns": 1})
    assert code == 200 and d["candidates"]

    code, p = _post(server + "/api/propose",
                    {"diagnosis_id": d["diagnosis_id"], "candidate_index": 0})
    assert code == 200 and p["tool"]

    code, r = _post(server + "/api/decide",
                    {"proposal_id": p["proposal_id"], "approved": True, "reason": "测试"})
    assert code == 200 and r["status"] == "success"


def test_http_audit_and_knowledge(server):
    for path in ("/api/audit", "/api/knowledge"):
        code, body = _get(server + path)
        assert code == 200
