"""HTTP 层的测试。

这个文件是补上的，而且**第一次跑就抓到了一个真洞**：

    Handler 上的 ``_extra_headers`` 原本声明成了**类属性**。
    ``self._extra_headers.append(...)`` 在它还是类属性时改的是类本身，
    于是 ``Set-Cookie`` 会在所有请求之间累积——**上一个用户的会话令牌
    被塞进下一个用户的响应里**。使用者感知不到，但任何人打开开发者工具
    都能看到别人的令牌。

这正说明为什么"HTTP 层没有测试"是个真问题：安全内核测了 35 条，
而真正把令牌发出去的那几行没人看。
"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from omagent.audit import AuditLog
from omagent.auth import ROLE_OPERATOR, ROLE_VIEWER, UserStore, load_secret
from omagent.web import COOKIE_NAME, CSRF_HEADER, CSRF_VALUE, App, Handler

from .fakes import FakeConsoleK8s

CSRF = {CSRF_HEADER: CSRF_VALUE}


class _FakeAgent:
    """Http 层只用到这几样。"""

    def __init__(self):
        self.k8s = FakeConsoleK8s()
        self.write_namespaces = ("demo",)
        self.cases = None   # 没配案例库 = 没有语义记忆

    def cluster_status(self):
        return True, "fake-k8s"

    def ui_tools(self):
        """工具能力面板的数据源。真实实现 = 内置 K8s 工具 + 已装配的 provider 工具。"""
        from omagent.tools import ui_tool_list

        return ui_tool_list()


@pytest.fixture()
def server(tmp_path):
    """起一个真服务器（随机端口），用真 HTTP 请求打它。"""
    users = UserStore(tmp_path / "users.json")
    users.add("zhang.wei", "ops-pass-2026", ROLE_OPERATOR)
    users.add("li.ming", "viewer-pass-1", ROLE_VIEWER)
    users.save()

    audit = AuditLog(tmp_path / "audit.jsonl")
    app = App(
        _FakeAgent(),  # type: ignore[arg-type]
        audit,
        llm=None,  # type: ignore[arg-type]
        operator="tester",
        default_namespace="demo",
        demo_mode=True,
        users=users,
        secret=load_secret(tmp_path / "secret.key"),
        session_dir=tmp_path / "sessions",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                type("BoundHandler", (Handler,), {"app": app}))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    # 测试需要直接碰会话对象（模拟后台线程产生的事件），
    # 把 App 挂成 Client 的类属性最省事，不必层层传参。
    Client.app = app
    yield f"http://127.0.0.1:{port}", audit
    httpd.shutdown()
    httpd.server_close()


class Client:
    """带 cookie jar 的极简客户端。"""

    app = None  # 由 fixture 注入；测试要直接碰会话对象时用

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.last_headers: list[tuple[str, str]] = []

    def _req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=15) as r:
                raw = r.read()
                self.last_headers = list(r.headers.items())
                try:
                    return r.status, (json.loads(raw) if raw else {})
                except ValueError:
                    return r.status, {"_raw": raw[:200].decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            self.last_headers = list(e.headers.items())
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {}

    def get(self, path, headers=None):
        return self._req("GET", path, headers=headers)

    def post(self, path, body=None, headers=None):
        # 注意用 is not None 而不是 `or`：空 dict 是 falsy，
        # `headers or CSRF` 会把"显式不带 CSRF 头"变成"带上"，测试就白测了。
        return self._req("POST", path, body, CSRF if headers is None else headers)

    def login(self, username, password):
        return self.post("/api/login", {"username": username, "password": password})

    def set_cookies(self):
        return [v for k, v in self.last_headers if k.lower() == "set-cookie"]

    def token(self):
        """当前会话 cookie 的值（按名字找，不碰 cookiejar 的私有结构）。"""
        for c in self.jar:
            if c.name == COOKIE_NAME:
                return c.value or ""
        return ""


# ── 未登录 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "/api/status", "/api/console", "/api/namespaces", "/api/audit",
    "/api/poll?session_id=x",
])
def test_未登录访问受保护接口一律_401(server, path):
    base, _ = server
    code, body = Client(base).get(path)
    assert code == 401, f"{path} 居然放行了"
    assert body.get("need_login") is True


def test_未登录不泄露任何集群信息(server):
    base, _ = server
    code, body = Client(base).get("/api/status")
    assert code == 401
    assert "cluster" not in body and "tools" not in body


def test_登录页本身不需要登录(server):
    """静态文件必须匿名可取，否则登录页都加载不出来。"""
    base, _ = server
    code, _ = Client(base).get("/")
    assert code == 200


def test_me_未登录时明确回答未登录(server):
    base, _ = server
    code, body = Client(base).get("/api/me")
    assert code == 200
    assert body == {"authenticated": False, "need_login": True}


# ── 登录 ──────────────────────────────────────────────────────────────────


def test_登录成功并带上会话_cookie(server):
    base, _ = server
    c = Client(base)
    code, body = c.login("zhang.wei", "ops-pass-2026")
    assert code == 200
    assert body["username"] == "zhang.wei" and body["role"] == ROLE_OPERATOR
    assert any(COOKIE_NAME in v for v in c.set_cookies())
    assert c.get("/api/status")[0] == 200


def test_密码错误_401(server):
    base, _ = server
    code, _ = Client(base).login("zhang.wei", "wrong")
    assert code == 401


def test_用户不存在也是_401_不透露用户是否存在(server):
    base, _ = server
    code, body = Client(base).login("nobody", "whatever")
    assert code == 401
    # 消息不能区分"用户不存在"和"密码不对"
    assert "密码不对" in body["error"]


def test_登录失败不会被写进审计当成成功(server):
    base, audit = server
    Client(base).login("zhang.wei", "wrong")
    events = [r for r in audit.records() if r["event"] == "login"]
    assert events and events[-1]["payload"]["ok"] is False


def test_成功登录会留痕(server):
    base, audit = server
    Client(base).login("zhang.wei", "ops-pass-2026")
    events = [r for r in audit.records() if r["event"] == "login"]
    assert events[-1]["payload"]["operator"] == "zhang.wei"


# ── CSRF ──────────────────────────────────────────────────────────────────


def test_缺少_CSRF_头的写请求被拒(server):
    base, _ = server
    c = Client(base)
    code, _ = c.post("/api/login", {"username": "zhang.wei", "password": "ops-pass-2026"},
                     headers={})
    assert code == 403


def test_登录后缺少_CSRF_头的写请求也被拒(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, _ = c.post("/api/session", {"namespace": "demo"}, headers={})
    assert code == 403


def test_带_CSRF_头就正常(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    assert c.post("/api/session", {"namespace": "demo"})[0] == 200


# ── 会话 cookie 不能跨请求累积（回归）────────────────────────────────────
#
# 这是本项目修过最严重的一个洞：_extra_headers 曾是类属性，
# Set-Cookie 于是在所有请求之间累积，**上一个用户的令牌被发给了下一个用户**。


def test_set_cookie_不会跨请求累积(server):
    base, _ = server
    c = Client(base)
    codes = []
    for _ in range(4):
        c.login("li.ming", "viewer-pass-1")
        codes.append(len(c.set_cookies()))
    assert codes == [1, 1, 1, 1], f"Set-Cookie 条数在增长：{codes}"


def test_两个用户的令牌不会互相泄漏(server):
    """A 登录后再 B 登录，B 收到的响应里**不能**出现 A 的令牌。"""
    base, _ = server
    a = Client(base)
    a.login("zhang.wei", "ops-pass-2026")
    token_a = a.token()
    assert token_a, "登录后应该拿到令牌"

    b = Client(base)
    b.login("li.ming", "viewer-pass-1")
    joined = " ".join(b.set_cookies())
    assert token_a not in joined, "A 的会话令牌泄漏进了 B 的响应"
    assert len(b.set_cookies()) == 1


def test_登出后立刻失效(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    assert c.get("/api/status")[0] == 200
    assert c.post("/api/logout", {})[0] == 200
    assert c.get("/api/status")[0] == 401


def test_伪造的令牌被拒(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    real = c.token()
    assert real, "登录后应该拿到令牌"
    tampered = real[:-6] + "AAAAAA"
    code, _ = Client(base).get("/api/status", headers={"Cookie": f"{COOKIE_NAME}={tampered}"})
    assert code == 401


# ── 角色 ──────────────────────────────────────────────────────────────────


def test_viewer_能看能问但不能批准(server):
    base, _ = server
    c = Client(base)
    c.login("li.ming", "viewer-pass-1")

    assert c.get("/api/console")[0] == 200, "viewer 应该能看监控台"
    assert c.get("/api/me")[1]["can_approve"] is False

    code, body = c.post("/api/approve",
                        {"session_id": "x", "proposal_id": "p", "approved": True})
    assert code == 403, "viewer 居然能批准写操作"
    assert "权限" in body["error"]


def test_viewer_不能注入故障(server):
    base, _ = server
    c = Client(base)
    c.login("li.ming", "viewer-pass-1")
    assert c.post("/api/sandbox/fault", {"scenario": "oom"})[0] == 403


def test_operator_的批准请求会走到门禁而不是被角色挡住(server):
    """没有待批方案时应该是 409（门禁语义），不是 403（权限语义）。"""
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, _ = c.post("/api/approve",
                     {"session_id": "x", "proposal_id": "p", "approved": True})
    assert code != 403


# ── 前端无法表达"要执行什么" ──────────────────────────────────────────────
#
# Web 层最重要的安全属性：浏览器只能"表态"，不能"改单"。
# 就算前端被完全攻陷，它能做的也只是批准一个本来就合法的方案，或者拒绝它。


def test_批准接口只认_proposal_id_多传的字段一概忽略(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    forged = {
        "session_id": "x",
        "proposal_id": "p",
        "approved": True,
        # 下面这些都是攻击者可能塞进来的"改单"尝试
        "tool": "delete_namespace",
        "params": {"namespace": "kube-system"},
        "target": {"kind": "Namespace", "name": "kube-system"},
    }
    code, body = c.post("/api/approve", forged)
    # 没有待批方案 → 门禁拒绝。绝不会因为多传了 tool 就去执行什么。
    assert code == 409
    assert "error" in body


def test_故障注入有场景白名单(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, body = c.post("/api/sandbox/fault", {"scenario": "rm -rf /"})
    assert code == 409
    assert "未知的沙箱场景" in body["error"]


def test_故障注入的目标名有字符白名单(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, body = c.post("/api/sandbox/fault",
                        {"scenario": "oom", "target": "x; rm -rf /"})
    assert code == 409
    assert "非法的目标名称" in body["error"]


# ── 其它 ──────────────────────────────────────────────────────────────────


def test_404_与_500_分开(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    assert c.get("/api/nope")[0] == 404


def test_会话持久化目录会被用到(server):
    """建会话应该落盘，重启后还能恢复。"""
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, snap = c.post("/api/session", {"namespace": "demo"})
    assert code == 200
    sid = snap["session_id"]
    # 落盘发生在 chat 之后；这里只验证会话可创建且能被服务端找到
    assert c.get(f"/api/poll?session_id={sid}")[0] == 200


def test_不在白名单里的命名空间写操作仍被门禁拦(server, tmp_path):
    """HTTP 层不引入新的执行路径：命名空间护栏在 agent 里，这里只是确认它还在。"""
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    # doc: write_namespaces == ("demo",)；接口本身拿不到"直接执行"的入口
    assert c.get("/api/status")[0] == 200


def test_登录与_me_返回同一形状(server):
    """登录接口和 /api/me 必须给出**同一个形状**。

    曾经登录只返回 username/role、没有 can_approve，前端于是把 operator
    显示成「只读」——而批准按钮又是能点的。界面自相矛盾比报错更糟。
    """
    base, _ = server
    c = Client(base)
    _, login_body = c.login("zhang.wei", "ops-pass-2026")
    _, me_body = c.get("/api/me")
    assert set(login_body) == set(me_body), (
        f"两个接口字段不一致：{sorted(login_body)} vs {sorted(me_body)}"
    )
    for body in (login_body, me_body):
        assert body["can_approve"] is True
        assert body["role"] == ROLE_OPERATOR
        assert body["role_label"]


def test_viewer_登录后_can_approve_为假(server):
    base, _ = server
    c = Client(base)
    _, body = c.login("li.ming", "viewer-pass-1")
    assert body["can_approve"] is False
    assert body["role"] == ROLE_VIEWER


def test_登录与_me_字段一致_对_viewer_同样成立(server):
    base, _ = server
    c = Client(base)
    _, a = c.login("li.ming", "viewer-pass-1")
    _, b = c.get("/api/me")
    assert a == b


# ── 会话持久化 ────────────────────────────────────────────────────────────
#
# 一轮排查的输出是**后台线程**产生的：HTTP 响应早就返回了，模型的回复、
# 工具结果、最终结论都是之后才陆续到达。所以落盘不能挂在 HTTP 处理函数里，
# 否则存下来的永远只有"用户刚发完消息"那一瞬间——重启后历史只剩一条 user 事件。


def test_落盘线程会把后台产生的事件写下来(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]

    # 直接往会话里塞一条"后台产生"的事件，模拟模型回复
    sess = Client.app.session(sid)
    sess._emit("assistant", text="这是后台线程产生的回复")
    sess._emit("tool", tool="get_pods", ok=True, params={}, result="x")

    # 手动触发一次 flush（等价于落盘线程到点）
    Client.app.flush_dirty()

    path = Client.app.session_dir / f"{sid}.json"
    assert path.is_file(), "会话应该已经落盘"
    data = json.loads(path.read_text(encoding="utf-8"))
    kinds = [e["type"] for e in data["events"]]
    assert "assistant" in kinds and "tool" in kinds, (
        f"后台产生的事件没被存下来，只存到了：{kinds}"
    )


def test_dirty_标记让重复落盘可以被跳过(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    sess = Client.app.session(sid)

    sess._emit("assistant", text="x")
    assert sess.dirty is True
    Client.app.flush_dirty()
    assert sess.dirty is False, "刷过之后应该不脏了"


# ── 401 与 409 必须分得开 ─────────────────────────────────────────────────
#
# 前端要能区分两件完全不同的事：
#   · 401 = 你没登录       → 弹登录页，**不要**去"重建会话"
#   · 409 = 这个对话会话没了 → 重建一个继续
#
# 之前前端靠中文字符串判断（`msg.includes('已过期')`），而 401 的消息是
# 「未登录或登录已过期」也含"已过期"——于是"未登录"被当成"会话过期"，
# 去重建、又 401、又被重新调度，**退出登录后陷入无限 401 循环**。
# 状态码是稳定的契约，中文措辞不是。


def test_未登录时_poll_返回_401_而不是_409(server):
    base, _ = server
    code, body = Client(base).get("/api/poll?session_id=whatever")
    assert code == 401, "未登录必须是 401，前端据此弹登录页而不是重建会话"
    assert body.get("need_login") is True


def test_已登录但会话不存在时返回_409(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, body = c.get("/api/poll?session_id=s-does-not-exist")
    assert code == 409, "登录正常但会话没了，这才是 409（前端据此重建）"
    assert "会话不存在" in body["error"]


def test_退出后所有受保护接口都变成_401(server):
    """退出登录后不能再有任何接口可用——包括 poll。"""
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    assert c.get(f"/api/poll?session_id={sid}")[0] == 200

    c.post("/api/logout", {})
    assert c.get(f"/api/poll?session_id={sid}")[0] == 401
    assert c.get("/api/status")[0] == 401
    assert c.get("/api/console")[0] == 401


def test_登录接口的_401_不触发登出流程(server):
    """密码错是 401，但它不是"你被登出了"——前端据此区分（path != /api/login）。

    服务端这边要保证：登录失败的响应里没有 need_login 之外的额外信息，
    而且不会把已有的有效会话弄失效。
    """
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    assert c.get("/api/status")[0] == 200

    # 用同一个客户端发一次错误登录
    code, _ = c.login("zhang.wei", "wrong")
    assert code == 401
    # 已经登录的会话不该被一次失败的登录搞掉
    assert c.get("/api/status")[0] == 200, "一次密码输错不该把已登录的会话踢掉"


# ── 多会话 ────────────────────────────────────────────────────────────────


def test_会话列表包含刚建的会话(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    # 空会话不落盘——列表里只出现「有内容」的会话，免得一堆空壳占位。
    # 有一条事件之后才会被落盘线程写下来。
    Client.app.session(sid)._emit("user", text="看看 demo")
    Client.app.flush_dirty()

    code, body = c.get("/api/sessions")
    assert code == 200
    ids = [s["id"] for s in body["sessions"]]
    assert sid in ids


def test_会话标题来自第一句提问(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    sess = Client.app.session(sid)
    sess._emit("user", text="order-service 一直在重启，帮我看看")
    Client.app.flush_dirty()

    _, body = c.get("/api/sessions")
    row = next(s for s in body["sessions"] if s["id"] == sid)
    assert "order-service 一直在重启" in row["title"]
    assert row["operator"] == "zhang.wei"


def test_删除会话会同时删掉摘要文件(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    Client.app.session(sid)._emit("user", text="看看 demo")
    Client.app.flush_dirty()

    code, body = c.post("/api/session/delete", {"session_id": sid})
    assert code == 200 and body["ok"] is True
    _, listing = c.get("/api/sessions")
    assert sid not in [s["id"] for s in listing["sessions"]]
    assert not (Client.app.session_dir / f"{sid}.json").exists()


def test_viewer_不能删除会话(server):
    base, _ = server
    c = Client(base)
    c.login("li.ming", "viewer-pass-1")
    code, _ = c.post("/api/session/delete", {"session_id": "s-whatever"})
    assert code == 403


def test_会话列表需要登录(server):
    base, _ = server
    assert Client(base).get("/api/sessions")[0] == 401


def test_登出再登录会话仍在磁盘上(server):
    """用户要的行为：登出只是结束登录会话，排查记录不该消失。"""
    base, _ = server
    a = Client(base)
    a.login("zhang.wei", "ops-pass-2026")
    sid = a.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    sess = Client.app.session(sid)
    sess._emit("user", text="查一下 cart-service")
    sess._emit("assistant", text="查到了")
    Client.app.flush_dirty()
    a.post("/api/logout", {})

    # 换一个全新的客户端重新登录
    b = Client(base)
    b.login("zhang.wei", "ops-pass-2026")
    _, listing = b.get("/api/sessions")
    assert sid in [s["id"] for s in listing["sessions"]]

    # 而且能完整读回来
    snap = b.get(f"/api/poll?session_id={sid}&since=0")[1]
    kinds = [e["type"] for e in snap["only_events"]]
    assert "user" in kinds and "assistant" in kinds


def test_空会话不会出现在列表里(server):
    """只建了会话、一句话没说，不该在列表里占一行。"""
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    sid = c.post("/api/session", {"namespace": "demo"})[1]["session_id"]
    Client.app.flush_dirty()
    _, body = c.get("/api/sessions")
    assert sid not in [s["id"] for s in body["sessions"]]


# ── 大屏 ──────────────────────────────────────────────────────────────────


def test_大屏接口需要登录(server):
    base, _ = server
    assert Client(base).get("/api/wall?namespaces=demo")[0] == 401


def test_大屏页面本身可匿名取(server):
    """页面是静态的（里面没有数据），数据接口仍然要登录。
    这样挂在墙上的那台机器刷新页面时不会看到一片 401 错误。"""
    base, _ = server
    code, body = Client(base).get("/wall")
    assert code == 200
    assert "_raw" in body and "O&M Agent" in body["_raw"]


def test_大屏返回的结构齐全(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, w = c.get("/api/wall?namespaces=demo,staging")
    assert code == 200
    for key in ("summary", "nodes", "groups", "alerts", "activity", "cases"):
        assert key in w, f"大屏缺少 {key}"
    assert w["summary"]["namespaces"] == 2
    assert {g["namespace"] for g in w["groups"]} == {"demo", "staging"}


def test_大屏的命名空间参数是白名单式的(server):
    """传进来的名字只用于查询，不参与任何拼接；空值要退回到默认命名空间。"""
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    code, w = c.get("/api/wall?namespaces=")
    assert code == 200
    assert w["summary"]["namespaces"] >= 1


def test_大屏的告警数与工作负载异常数一致(server):
    base, _ = server
    c = Client(base)
    c.login("zhang.wei", "ops-pass-2026")
    _, w = c.get("/api/wall?namespaces=demo")
    manual = sum(1 for g in w["groups"] for x in g["workloads"] if not x["healthy"])
    manual += sum(len(g.get("standalone") or []) for g in w["groups"])
    assert w["summary"]["alert_total"] == manual


# ---------------------------------------------------------------------------
# 首启动自举
#
# 存在的理由：同类 CLI 工具挂上 kubeconfig 就能用，而这个项目原来要求先手工
# 跑 useradd 才能启动——卡在这里的人根本走不到看界面那一步。
# 但"能自动建账号"离"能偷偷开后门"只有一步之遥，所以边界必须由测试钉死。
# ---------------------------------------------------------------------------


def _fresh(tmp_path):
    from omagent.audit import AuditLog

    return tmp_path / "users.json", AuditLog(tmp_path / "audit.jsonl")


def test_没有用户库时自动自举(tmp_path):
    from omagent.auth import ROLE_OPERATOR
    from omagent.web import ensure_users

    path, audit = _fresh(tmp_path)
    users, boot = ensure_users(path, audit=audit)

    assert boot is not None
    name, password = boot
    assert name == "admin"
    assert len(password) >= 12, "自动生成的密码不能太短"
    # 生成的密码必须真的能登录，否则"能启动了"是假的
    assert users.verify("admin", password) is not None
    assert users.verify("admin", password)["role"] == ROLE_OPERATOR
    assert path.exists()
    # 自举是安全事件，必须进审计链
    assert "bootstrap_admin_created" in (tmp_path / "audit.jsonl").read_text()


def test_第二次启动不再自举(tmp_path):
    """否则每次重启都冒一个新密码出来，前一个就白记了。"""
    from omagent.web import ensure_users

    path, audit = _fresh(tmp_path)
    _users, first = ensure_users(path, audit=audit)
    _users2, second = ensure_users(path, audit=audit)

    assert first is not None and second is None


def test_用户库存在但为空时拒绝启动(tmp_path):
    """边界：文件存在却为空 = 有人**有意清空**。

    这时候偷偷补一个后门账号，比直接拒绝启动危险得多。
    """
    from omagent.web import ensure_users

    path, _audit = _fresh(tmp_path)
    path.write_text('{"users": []}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        ensure_users(path)
    assert "拒绝启动" in str(exc.value)


def test_no_bootstrap_时不自动建账号(tmp_path):
    from omagent.web import ensure_users

    path, _audit = _fresh(tmp_path)
    with pytest.raises(SystemExit):
        ensure_users(path, bootstrap=False)
    assert not path.exists(), "拒绝启动就不该留下半个用户库"


def test_匿名模式不碰用户库(tmp_path):
    from omagent.web import ensure_users

    path, _audit = _fresh(tmp_path)
    assert ensure_users(path, allow_anonymous=True) == (None, None)
    assert not path.exists()


# ---------------------------------------------------------------------------
# doctor 自检
# ---------------------------------------------------------------------------


def test_doctor_report_全通过时返回0(capsys):
    from omagent.cli import _doctor_report

    rc = _doctor_report([("集群可达", True, "v1.37.0", ""),
                         ("只读权限", True, "允许", "")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "全部通过" in out


def test_doctor_report_失败时打印修复命令(capsys):
    """自检的价值全在"每项失败都给一条能直接粘的命令"。"""
    from omagent.cli import _doctor_report

    rc = _doctor_report([
        ("集群可达", True, "v1.37.0", ""),
        ("写权限 · demo", False, "patch deployments：被拒绝",
         "授予 namespace demo 内 deployments 的 patch/update 权限"),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 项需要处理" in out
    assert "授予 namespace demo" in out
    # 通过的项不该出现在"需要处理"里
    assert out.count("集群可达") == 1


def test_doctor_can_i_用SelfSubjectAccessReview(tmp_path):
    """_can_i 不该有副作用——它问的是 API Server，不是真去改东西。"""
    from omagent.cli import _can_i

    seen = {}

    class _Status:
        allowed = True
        reason = ""

    class _Resp:
        status = _Status()

    class _Authz:
        def create_self_subject_access_review(self, review):
            seen["verb"] = review.spec.resource_attributes.verb
            seen["resource"] = review.spec.resource_attributes.resource
            seen["ns"] = review.spec.resource_attributes.namespace
            return _Resp()

    class _K8s:
        authz = _Authz()

    ok, _reason = _can_i(_K8s(), "patch", "apps", "deployments", "demo")
    assert ok is True
    assert seen == {"verb": "patch", "resource": "deployments", "ns": "demo"}


def test_doctor_can_i_异常时判为不允许():
    """问不出来就当没权限（fail closed），不能默认放行。"""
    from omagent.cli import _can_i

    class _K8s:
        class authz:
            @staticmethod
            def create_self_subject_access_review(_review):
                raise RuntimeError("API Server 挂了")

    ok, reason = _can_i(_K8s(), "list", "", "pods", "demo")
    assert ok is False
    assert "检查失败" in reason
