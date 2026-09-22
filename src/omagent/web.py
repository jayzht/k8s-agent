"""Web 审批界面：HTTP 服务 + JSON API。

**安全设计（本模块最重要的部分）**

Web 层不引入任何新的执行路径。它只是安全内核的一层薄封装，
而且刻意让浏览器**无法**表达"要执行什么动作"：

1. ``/api/diagnose`` 把诊断结果存在**服务端**，只回传一个 ``diagnosis_id``
2. ``/api/propose`` 只接受 ``{diagnosis_id, candidate_index}``——
   候选动作由服务端从存储里取，浏览器无法伪造工具名或参数
3. ``/api/decide`` 只接受 ``{proposal_id, approved, reason}``——
   待执行的方案同样存在服务端，浏览器只能"表态"，不能"改单"

结果是：即使前端被完全攻陷，攻击者能做的也只是**批准一个本来就合法的方案**，
或者**拒绝它**。这正好复刻了真实门禁的语义——批准凭证与方案严格绑定
（见 ``agent.execute()`` 中的 proposal_id 校验）。

服务仅监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent import TOOLS, GateViolation, OpsAgent, Refusal
from .audit import AuditLog, new_trace_id
from .knowledge import KnowledgeStore, extract_keywords
from .k8s import K8sClient, K8sUnavailable
from .models import Decision, Evidence, Proposal
from .planner import LLMPlanner, RuleBasedPlanner
from .policy import Policy

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"

# 诊断/方案在服务端的存活时间（秒）。过期后浏览器必须重新诊断。
STORE_TTL = 1800


class _Store:
    """带 TTL 的通用对象存储（诊断结果与待审批方案各用一个实例）。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._items[key] = (time.time(), value)

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts > STORE_TTL:
                del self._items[key]
                return None
            return value

    def pop(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.pop(key, None)
            return entry[1] if entry else None

    def purge(self) -> None:
        now = time.time()
        with self._lock:
            for k in [k for k, (ts, _) in self._items.items() if now - ts > STORE_TTL]:
                del self._items[k]


class OpsWebApp:
    """持有 agent 与各种存储；由 HTTP handler 调用。"""

    def __init__(
        self,
        agent: OpsAgent,
        audit: AuditLog,
        knowledge: KnowledgeStore,
        operator: str = "web@example.com",
        default_planner: str = "rule",
        demo_mode: bool = False,
    ):
        self.agent = agent
        self.audit = audit
        self.knowledge = knowledge
        self.operator = operator
        self.default_planner = default_planner
        self.demo_mode = demo_mode
        self.diagnoses = _Store()
        self.proposals = _Store()
        self.lock = threading.Lock()  # 串行化 propose/execute，避免并发写同一资源

    # ------------------------------------------------------------------ 只读

    def status(self) -> dict[str, Any]:
        from .config import llm_status

        ok, info = self.agent.cluster_status()
        return {
            "cluster": {"ok": ok, "info": info},
            "llm": llm_status(),
            "policy": self.agent.policy.summary(),
            "tools": {
                name: {"mutating": spec.mutating, "description": spec.description}
                for name, spec in sorted(TOOLS.items())
            },
            "operator": self.operator,
            "demo": self.demo_mode,
        }

    def workloads(self, namespace: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for kind, lister in (
            ("deployment", self.agent.k8s.apps.list_namespaced_deployment),
            ("statefulset", self.agent.k8s.apps.list_namespaced_stateful_set),
        ):
            try:
                for item in lister(namespace).items:
                    labels = dict(item.metadata.labels or {})
                    out.append(
                        {
                            "namespace": namespace,
                            "kind": kind,
                            "name": item.metadata.name,
                            "replicas": item.spec.replicas,
                            "protected": self._is_protected(labels),
                            "labels": labels,
                        }
                    )
            except Exception:  # noqa: BLE001
                continue
        return out

    def _is_protected(self, labels: dict[str, str]) -> bool:
        return any(labels.get(k) == v for k, v in self.agent.policy.protected_labels.items())

    # -------------------------------------------------------------- 诊断/方案

    def diagnose(
        self, namespace: str, workload: str, kind: str, planner: str = "", turns: int = 1
    ) -> dict[str, Any]:
        planner_name = planner or self.default_planner
        if planner_name == "llm":
            eng: Any = LLMPlanner(self.agent.k8s)
            if not eng.available:
                eng = RuleBasedPlanner(self.agent.k8s)
                planner_name = "rule(降级)"
        else:
            eng = RuleBasedPlanner(self.agent.k8s)

        trace = new_trace_id()
        self.audit.log_intent(trace, self.operator, f"[web] 诊断 {kind}/{workload} @ {namespace}")

        investigation: list[dict[str, Any]] = []
        if turns > 1:
            from .loop import AgentLoop

            run = AgentLoop(eng, self.agent, max_turns=turns).run(namespace, workload, kind)
            diag = run.diagnosis
            investigation = [
                {"turn": t.turn, "tool": t.tool, "params": t.params,
                 "result": t.result, "error": t.error}
                for t in run.turns
            ]
        else:
            diag = eng.diagnose(namespace, workload, kind)

        assert diag is not None
        evidence_text = " ".join(f"{e.ref} {e.detail}" for e in diag.evidence)
        hint = self.knowledge.hint_for(
            diag.matched_signature, extract_keywords(evidence_text), workload
        )

        self.audit.log_diagnosis(
            trace, self.operator, diag.conclusion, [e.to_dict() for e in diag.evidence]
        )

        diag_id = f"diag-{int(time.time() * 1000):x}"
        self.diagnoses.put(diag_id, {"diag": diag, "trace": trace, "namespace": namespace,
                                     "workload": workload, "kind": kind,
                                     "evidence_text": evidence_text})

        return {
            "diagnosis_id": diag_id,
            "planner": planner_name,
            "signature": diag.matched_signature,
            "conclusion": diag.conclusion,
            "confidence": diag.confidence,
            "evidence": [e.to_dict() for e in diag.evidence],
            "findings": diag.findings,
            "history_hint": hint,
            "investigation": investigation,
            "candidates": [
                {"index": i, "tool": c.tool, "params": c.params,
                 "rationale": c.rationale, "note": c.note,
                 "mutating": TOOLS[c.tool].mutating if c.tool in TOOLS else True}
                for i, c in enumerate(diag.candidates)
            ],
        }

    def ask(self, text: str, namespace_hint: str = "") -> dict[str, Any]:
        """自然语言入口。

        意图解析只产出结构化参数，解析完直接复用 ``diagnose`` —— 不新增执行路径。
        解析不出来时返回 ``resolved=False`` 与原因，而不是猜一个目标。
        """
        from .intent import IntentParser, IntentUnresolved

        parser = IntentParser(self.agent.k8s, self.agent.policy)
        try:
            intent = parser.parse(text, namespace_hint)
        except IntentUnresolved as exc:
            return {"resolved": False, "message": str(exc), "raw": text}

        out = self.diagnose(
            intent.namespace, intent.workload, intent.kind,
            self.default_planner, int(intent.turns),
        )
        out["intent"] = intent.to_dict()
        out["resolved"] = True
        return out

    def propose(self, diagnosis_id: str, candidate_index: int) -> dict[str, Any]:
        stored = self.diagnoses.get(diagnosis_id)
        if stored is None:
            raise GateViolation("诊断结果不存在或已过期，请重新诊断")

        diag = stored["diag"]
        if not (0 <= candidate_index < len(diag.candidates)):
            raise GateViolation("候选动作编号越界")
        cand = diag.candidates[candidate_index]

        with self.lock:
            prop = self.agent.propose(
                cand.tool,
                cand.params,
                rationale=cand.rationale,
                note=cand.note,
                evidence=diag.evidence,
                trace_id=stored["trace"],
            )
        self.proposals.put(prop.proposal_id, {"prop": prop, "trace": stored["trace"],
                                              **{k: stored[k] for k in
                                                 ("namespace", "workload", "kind",
                                                  "evidence_text")}})
        return _proposal_view(prop)

    def decide(self, proposal_id: str, approved: bool, reason: str) -> dict[str, Any]:
        stored = self.proposals.pop(proposal_id)
        if stored is None:
            raise GateViolation("方案不存在或已过期（可能已处理过）——请重新诊断")
        prop: Proposal = stored["prop"]

        decision = Decision(
            proposal_id=prop.proposal_id,
            approved=approved,
            operator=self.operator,
            reason=reason or ("web 界面确认" if approved else "web 界面拒绝"),
        )
        self.audit.log_decision(stored["trace"], decision)

        with self.lock:
            res = self.agent.execute(prop, decision, trace_id=stored["trace"])

        # 知识沉淀：把本次「症状→根因→处置→结果」落库
        self.knowledge.record_from_diagnosis(
            namespace=stored["namespace"],
            workload=stored["workload"],
            kind=stored["kind"],
            signature=prop.tool,
            conclusion=prop.rationale,
            evidence_text=stored["evidence_text"],
            treatment=f"{prop.tool} {prop.params}",
            treatment_tier=prop.effective_tier.value,
            outcome=res.status,
            outcome_note=res.output or res.error,
            operator=self.operator,
        )
        return {"status": res.status, "output": res.output, "error": res.error,
                "duration_ms": res.duration_ms}

    # ------------------------------------------------------- 沙箱故障注入
    #
    # 说明：这不是 Agent 的动作，而是**扮演"某个开发推了一次坏变更"**。
    # 因此它刻意**不走安全门禁**——门禁管的是 Agent 能不能改生产，
    # 不管"把沙箱弄坏"这件事。但仍然做了三层限制：
    #   1) 场景名是**枚举白名单**，不接受任意命令
    #   2) 只操作策略允许的命名空间
    #   3) 仅在演示模式下开放（serve(mode="demo")）

    SANDBOX_SCENARIOS = ("oom", "crash", "image", "pending", "reset")

    def inject_fault(self, scenario: str) -> dict[str, Any]:
        import subprocess

        if scenario not in self.SANDBOX_SCENARIOS:
            raise GateViolation(
                f"未知的沙箱场景 {scenario!r}；可用：{', '.join(self.SANDBOX_SCENARIOS)}"
            )
        if not self.demo_mode:
            raise GateViolation("故障注入仅在演示模式下可用（启动时加 --demo）")

        script = ROOT / "sandbox" / "faults.sh"
        if not script.exists():
            raise GateViolation(f"找不到故障注入脚本：{script}")
        env = dict(os.environ)
        env["PATH"] = f"{ROOT / 'bin'}:{env.get('PATH', '')}"
        env.setdefault("KUBECONFIG", str(ROOT / "var" / "kubeconfig"))
        try:
            r = subprocess.run(
                ["/bin/bash", str(script), scenario],
                capture_output=True, text=True, timeout=180, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return {"scenario": scenario, "ok": False, "output": f"超时：{exc}"}
        out = (r.stdout or "") + (r.stderr or "")
        self.audit.append("sandbox_fault", {
            "scenario": scenario, "operator": self.operator, "exit": r.returncode,
        })
        return {"scenario": scenario, "ok": r.returncode == 0,
                "output": out[-2000:] or "（无输出）"}

    def refuse(self, rule: str, request: str = "") -> dict[str, Any]:
        r: Refusal = self.agent.refuse(request or f"web 请求：{rule}", rule)
        return {"rule_id": r.rule_id, "desc": r.desc, "hint": r.hint, "text": r.render()}

    def audit_tail(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.audit.records()[-limit:]

    def knowledge_view(self, limit: int = 10) -> dict[str, Any]:
        entries = self.knowledge.all()
        return {
            "stats": self.knowledge.stats(),
            "recent": [
                {"ts": e.ts, "signature": e.signature, "workload": e.workload,
                 "root_cause": e.root_cause, "treatment": e.treatment,
                 "outcome": e.outcome, "tier": e.treatment_tier}
                for e in entries[-limit:][::-1]
            ],
        }


def _proposal_view(prop: Proposal) -> dict[str, Any]:
    """给前端的方案视图。

    注意：返回的是**展示用**数据。真正执行时服务端用自己的 Proposal 对象，
    不会采信前端回传的任何字段。
    """
    # 冷却期是**临时**状态，告诉用户"还要等多久"比"被拒绝了"有用得多
    cooldown_left = 0
    for b in prop.breaches:
        if b.rule == "变更冷却":
            import re as _re
            m = _re.search(r"(\d+)\s*s", b.detail)
            if m:
                cooldown_left = int(m.group(1))
    return {
        **prop.to_dict(),
        "cooldown_left": cooldown_left,
        # 告诉界面：这个动作是"根治"还是"只是缓解"。
        # 不区分的话，用户点完看到"成功"却发现问题复发，会以为系统坏了。
        "is_mitigation": bool(prop.note) and ("缓解" in prop.note),
        "tier_label": prop.effective_tier.label,
        "confirm_strength": prop.effective_tier.confirm_strength,
        "requires_approval": prop.requires_approval,
        "blocked": prop.blocked,
        "impact_rows": [
            ["影响副本", f"{prop.impact.replicas} 个" if prop.impact.replicas else "未知"],
            ["影响 Pod 数", f"{prop.impact.pods_restarted} 个"],
            ["是否有状态", "是（StatefulSet）" if prop.impact.stateful else "否"],
            ["是否单点", "⚠️ 是" if prop.impact.single_point else "否"],
            ["持久卷挂载", "⚠️ 是" if prop.impact.has_pvc else "否"],
            ["PDB 约束", prop.impact.pdb or "未知"],
            ["上游 Service", "、".join(prop.impact.upstream_deps) or "无"],
        ],
        "impact_notes": prop.impact.notes,
    }


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    app: OpsWebApp  # 由 serve() 注入

    server_version = "omagent"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        pass

    # ------------------------------------------------------------------ 工具

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            # `/` 走**简化演示页**（面向第一次接触运维的人），
            # 原来的高密度运维台移到 `/pro`。两个都保留：
            # 演示要的是"看懂"，日常用要的是"信息全"。
            if path in ("/", "/index.html", "/simple"):
                return self._static("simple.html")
            if path in ("/pro", "/pro/"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/") :])
            if path == "/api/status":
                return self._json(self.app.status())
            if path == "/api/audit":
                return self._json({"records": self.app.audit_tail()})
            if path == "/api/knowledge":
                return self._json(self.app.knowledge_view())
            if path == "/api/health":
                return self._json({"ok": True})
            return self._error("not found", 404)
        except Exception as exc:  # noqa: BLE001
            return self._error(str(exc), 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        try:
            if path == "/api/workloads":
                return self._json(
                    {"workloads": self.app.workloads(body.get("namespace", "demo"))}
                )
            if path == "/api/ask":
                return self._json(
                    self.app.ask(body.get("text", ""), body.get("namespace", ""))
                )
            if path == "/api/diagnose":
                return self._json(
                    self.app.diagnose(
                        body.get("namespace", "demo"),
                        body["workload"],
                        body.get("kind", "deployment"),
                        body.get("planner", ""),
                        int(body.get("turns", 1)),
                    )
                )
            if path == "/api/propose":
                return self._json(
                    self.app.propose(body["diagnosis_id"], int(body["candidate_index"]))
                )
            if path == "/api/decide":
                return self._json(
                    self.app.decide(
                        body["proposal_id"], bool(body.get("approved")), body.get("reason", "")
                    )
                )
            if path == "/api/sandbox/fault":
                return self._json(self.app.inject_fault(body.get("scenario", "")))
            if path == "/api/refuse":
                return self._json(
                    self.app.refuse(body.get("rule", "delete_namespace"), body.get("request", ""))
                )
            return self._error("not found", 404)
        except GateViolation as exc:
            # 门禁拒绝是**预期行为**，用 409 明确区分于 500
            return self._error(f"门禁拒绝：{exc}", 409)
        except KeyError as exc:
            return self._error(f"缺少参数：{exc}", 400)
        except Exception as exc:  # noqa: BLE001
            return self._error(str(exc), 500)

    def _static(self, name: str) -> None:
        # 防目录穿越：只允许 web/ 下的直接子文件
        if "/" in name or "\\" in name or name.startswith("."):
            return self._error("bad path", 400)
        target = (WEB_DIR / name).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._error("not found", 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    operator: str = "web@example.com",
    planner: str = "rule",
    kubeconfig: str | None = None,
    audit_path: str | None = None,
    knowledge_path: str | None = None,
    policy_path: str | None = None,
    demo: bool = False,
    cooldown: int | None = None,
) -> None:
    k8s = K8sClient(kubeconfig=kubeconfig)
    policy = Policy(policy_path) if policy_path else Policy()
    audit = AuditLog(audit_path or (ROOT / "var" / "audit.jsonl"))
    agent = OpsAgent(k8s, policy, audit, operator=operator)
    knowledge = KnowledgeStore(knowledge_path or (ROOT / "var" / "knowledge.jsonl"))

    app = OpsWebApp(agent, audit, knowledge, operator=operator,
                    default_planner=planner, demo_mode=demo)
    # 演示模式的冷却期默认缩短：生产用 300s 防抖动，
    # 但演示时"刚修完想再试一次就被拦 5 分钟"会让人以为程序坏了。
    if cooldown is not None:
        agent.policy.cooldown_seconds = int(cooldown)
    elif demo:
        agent.policy.cooldown_seconds = min(agent.policy.cooldown_seconds, 20)
    handler = type("BoundHandler", (Handler,), {"app": app})

    httpd = ThreadingHTTPServer((host, port), handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"O&M Agent Web 审批界面： http://{shown}:{port}")
    if host in ("0.0.0.0", "::"):
        print("  ⚠️ 已监听所有网卡——同网段可访问。仅用于演示/内网，勿暴露公网。")
    else:
        print("  （仅监听本机；如需外部访问请加 --host 0.0.0.0）")
    print(f"  变更冷却期：{agent.policy.cooldown_seconds}s"
          + ("（演示模式已缩短）" if demo and cooldown is None else ""))
    print("  所有写操作仍需在此界面人工确认；故障注入仅在 --demo 下可用。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
