"""评测自身的缺陷回归测试。

这里测的**不是诊断质量**（那要真模型真集群，见 ``omagent eval``），
而是评测配置本身有没有走样。两个缺陷都是上一轮实测暴露出来的：

缺陷 A：``eval`` 的 ``agent_factory`` 没传 ``providers=``，于是全部剧本是在
        "Prometheus / Loki / 变更历史全关掉"的配置下跑的——**评测没测真实配置**。
        而"依赖变慢"这类故障恰恰要靠指标才能定位。

缺陷 B：``netdelay`` / ``netpart`` 的题干把注入目标和它的对端都写进去了
        （"order-service 访问 postgres"），等于提前泄题；而 Agent 根本没有
        读 Chaos 对象的工具，所以这种题**必须泄题才可能过**——过了也不值钱。

⚠️ 这些测试同时是**护栏**：不许为了让那三条挂掉的剧本"好看"去放宽判据
（放宽 ``expect`` / 把 ``allow_none`` 改成 True / 删 ``forbid`` / 把题干改回泄题）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omagent import cli
from omagent.evals import SCENARIOS


# ---------------------------------------------------------------------------
# 缺陷 A：评测必须接上 provider
# ---------------------------------------------------------------------------


class _FakeK8s:
    """够 ``_build_providers`` 构造 provider 用的最小假客户端。

    ``ChangeHistoryProvider.available()`` 只会 list 一次 deployment；
    Prometheus / Loki 的可用性在下面被 patch 掉，不发真网络请求。
    """

    def __init__(self) -> None:
        self.apps = SimpleNamespace(
            list_namespaced_deployment=lambda *a, **k: None,
        )


def _eval_args(*extra: str):
    return cli.build_parser().parse_args(["eval", *extra])


def _factory(args, tmp_path):
    return cli.make_eval_agent_factory(
        args,
        kubeconfig=None,
        k8s_factory=_FakeK8s,
        audit_path=tmp_path / "eval-audit.jsonl",
        cases_path=tmp_path / "eval-cases.jsonl",
    )


@pytest.fixture()
def _fake_http_providers_up():
    """让 Prometheus / Loki 的可用性检查返回"可用"，不碰网络。"""
    with patch("omagent.observability.PrometheusProvider.available",
               return_value=(True, "fake prometheus")), \
         patch("omagent.observability.LokiProvider.available",
               return_value=(True, "fake loki")):
        yield


def test_eval_agent_factory_默认接上_provider(_fake_http_providers_up, tmp_path):
    """**默认**（不带任何开关）造出来的 agent 必须带 provider 工具。

    这条如果挂了，说明"评测测的配置"和"线上跑的配置"又分叉了——
    那个通过率就不再是对真实配置的度量。
    """
    agent = _factory(_eval_args(), tmp_path)()

    # 三个数据源都在：变更历史（纯 K8s，默认总有）+ Prometheus + Loki
    names = {p["name"] for p in agent.providers.status() if p["available"]}
    assert {"changes", "prometheus", "loki"} <= names, names

    # provider 工具真的进了工具清单
    for tool in ("get_recent_changes", "query_metrics", "get_alerts",
                 "get_targets", "query_logs"):
        assert agent.providers.spec(tool) is not None, tool

    # 41 个内置工具 + 5 个 provider 只读工具
    assert len(agent.tool_schema()) == 46

    # ⚠️ 历史 bug：provider 的只读工具被 session 当成写操作，
    #    一调用就报"未登记，拒绝生成方案"。这里把"只读"钉死。
    assert agent.is_mutating("query_metrics") is False


def test_eval_agent_factory_no_observability_不接_provider(_fake_http_providers_up, tmp_path):
    """``--no-observability`` 必须**真的**把它们摘掉——这是"有无数据源"的对照组。

    变更历史是纯 K8s、不需要外部系统，所以它**仍然在**（和 serve 语义一致）。
    """
    agent = _factory(_eval_args("--no-observability"), tmp_path)()

    names = {p["name"] for p in agent.providers.status() if p["available"]}
    assert names == {"changes"}, names

    assert agent.providers.spec("query_metrics") is None
    assert agent.providers.spec("query_logs") is None
    assert agent.providers.spec("get_alerts") is None
    assert agent.providers.spec("get_recent_changes") is not None
    assert len(agent.tool_schema()) == 42


def test_eval_和_serve_共用同一套_provider_参数():
    """``eval`` 的 provider 参数必须和 ``serve`` 同名同义。

    在 eval 里另写一份参数表迟早漂移——而"评测跑的配置"必须能一键复现
    "线上跑的配置"。所以这里直接断言两边的关键参数都存在。
    """
    parser = cli.build_parser()
    for sub in ("serve", "eval"):
        args = parser.parse_args([sub])
        assert args.obs_namespace == "monitoring"
        assert args.prometheus_url == ""
        assert args.loki_url == ""
        # 默认必须是**接上**（False = 不关），显式传 --no-observability 才关
        assert args.no_observability is False

    off = parser.parse_args(["eval", "--no-observability"])
    assert off.no_observability is True
    override = parser.parse_args([
        "eval", "--obs-namespace", "obs",
        "--prometheus-url", "http://p:9090",
        "--loki-url", "http://l:3100",
    ])
    assert (override.obs_namespace, override.prometheus_url,
            override.loki_url) == ("obs", "http://p:9090", "http://l:3100")


# ---------------------------------------------------------------------------
# 缺陷 B：题干不许泄题
# ---------------------------------------------------------------------------


#: 每个剧本的题干里**绝不能出现**的名字。
#: 左边是场景，右边是"注入目标 / 源服务 / 根因对象"。
_LEAKED_NAMES: dict[str, tuple[str, ...]] = {
    "netdelay": ("order-service", "postgres", "postgres-primary"),
    "netpart": ("order-service", "postgres", "postgres-primary"),
    "stress": ("api-gateway",),
}


@pytest.mark.parametrize("name", sorted(_LEAKED_NAMES))
def test_chaos_题干不点名任何服务(name):
    """题干只能是**用户视角的症状**，不能出现注入目标或它依赖的服务。

    人看到的是"下单页面慢"，不是"order-service 访问 postgres 慢"。
    后者等于把故障边界直接告诉模型——那样即使答对也不值钱。
    """
    sc = next(s for s in SCENARIOS if s.name == name)
    question = sc.question.lower()
    for leaked in _LEAKED_NAMES[name]:
        assert leaked.lower() not in question, f"{name} 题干泄题：出现了 {leaked!r}"
    # 注入目标自己也不能出现在题干里
    assert sc.target.lower() not in question
    # 点的是用户看到的入口，不是某个后端服务
    assert "页面" in sc.question or "商城" in sc.question


def test_chaos_题干去泄题后_判据没被放宽():
    """护栏：不许为了"让挂掉的剧本好看"而放宽判据。

    这三条的正解是**空的**（没有工具能删 Chaos 对象），只能靠"说清根因 +
    明说做不到"过关。放宽 expect / 把 allow_none 翻成 True 之外的改动、
    删 forbid，都会把这次实验的价值抹掉。
    """
    by_name = {s.name: s for s in SCENARIOS}
    for name in ("netdelay", "netpart", "stress"):
        sc = by_name[name]
        assert sc.expect == frozenset(), f"{name}: expect 被放宽了"
        assert sc.allow_none is True, f"{name}: allow_none 语义变了"
        assert sc.injector == "chaos"
        assert "rollout_restart" in sc.forbid
    # stress 那条"改限额治不好运行时注入"的陷阱必须留着
    assert "patch_resources" in by_name["stress"].forbid


def test_netdelay_mentions_不含拓扑泄题词():
    """``mentions`` 是"结论里该出现的关键词"。

    原来 netdelay 里有「下游」——那是照着泄题题干（谁访问谁）写的，
    等于把拓扑白送。去掉题干后它必须换成真正的根因词。
    """
    sc = next(s for s in SCENARIOS if s.name == "netdelay")
    assert "下游" not in sc.mentions
    assert "NetworkChaos" in sc.mentions
    assert "延迟" in sc.mentions


# ---------------------------------------------------------------------------
# 判据收紧之一：根因门禁（root_cause_all → Outcome.root_cause_ok）
#
# 背景：``expect=∅`` + ``allow_none=True`` 的场景里，"诊断对了所以不动手"和
# "诊断错了但也没动手"在旧判据下**完全分不出来**。上一轮 25/25 就是这么来的：
# stress 把根因说反了、noissue 编造了一个故障，两者都因为"没提写动作"而过。
# ---------------------------------------------------------------------------


def test_root_cause_all_为空时判据不生效():
    """**空元组 = 门禁不生效**：没配的老剧本判定一个字都不能变。

    如果这条挂了，说明这轮改动把全部剧本的判定都改了，掉的分就归不了因。
    """
    from omagent.evals import Outcome, root_cause_ok_for

    plain = [s for s in SCENARIOS if not s.root_cause_all]
    assert plain, "至少要有一批没配 root_cause_all 的剧本"
    for sc in plain:
        # 结论是空、是错的、是任意字符串，都不影响
        assert root_cause_ok_for(sc, "") is True, sc.name
        assert root_cause_ok_for(sc, "完全无关的胡话") is True, sc.name
    # 默认值也必须是空（新剧本不写这个字段就不受门禁）
    assert Outcome(scenario="oom").root_cause_ok is True


def test_root_cause_all_非空时缺词就不过():
    """非空时**必须全部命中**：缺任何一个词都判不过。"""
    from omagent.evals import root_cause_ok_for

    by_name = {s.name: s for s in SCENARIOS}
    for name in ("stress", "noissue", "netdelay", "netpart"):
        sc = by_name[name]
        assert sc.root_cause_all, f"{name} 没配根因门禁"
        # 全词命中 → 过
        assert root_cause_ok_for(sc, "".join(sc.root_cause_all)) is True, name
        # 少任何一个词 → 不过
        for i in range(len(sc.root_cause_all)):
            partial = "".join(w for j, w in enumerate(sc.root_cause_all) if j != i)
            assert root_cause_ok_for(sc, partial) is False, f"{name} 缺第 {i} 个词也过了"


def test_root_cause_ok_计入_passed():
    """门禁是**计入 passed 的判据**，不是参考指标。"""
    from omagent.evals import Outcome

    def make(**kw):
        base = dict(scenario="stress", investigated=True, trap_avoided=True,
                    action_ok=True, root_cause_ok=True)
        base.update(kw)
        return Outcome(**base)

    assert make().passed is True
    assert make(root_cause_ok=False).passed is False, "根因门禁没计入 passed"


def test_根因门禁的选词能拦下已知的错误结论():
    """**这条是本次收紧的核心回归**：选词必须真的卡住上一轮的错误结论。

    左边是上一轮报告里**实际跑出来的结论摘要**（原样摘录，不是编的）。
    它们当时都 PASS 了，因为 expect=∅ + allow_none，而 mentions 只是参考。
    """
    from omagent.evals import root_cause_ok_for

    by_name = {s.name: s for s in SCENARIOS}

    # stress：上一轮的真实结论，全程只说 CPU/HPA/探针，**一次没提 StressChaos**。
    stress_wrong = (
        "核心问题：api-gateway 的 HPA 卡在'想扩到 8、实际只有 6 个可用'，"
        "扩容扩不动，网关长期在 CPU 打满的状态下扛流量。"
        "新扩出来的 2 个 Pod 探针打错了端口。"
    )
    assert root_cause_ok_for(by_name["stress"], stress_wrong) is False, \
        "stress 的错误结论（没提 Chaos）必须判不过"

    # noissue：上一轮接上 provider 后的真实结论——把评测自己留下的
    # Loki/Prometheus 历史当成了"命名空间在被反复重建"，**编造了一个故障**。
    noissue_wrong = (
        "核心发现：整个 demo 命名空间在被反复重建。"
        "现在所有服务都是健康的，但集群其实一直在抖。"
    )
    assert root_cause_ok_for(by_name["noissue"], noissue_wrong) is False, \
        "noissue 编造故障的结论必须判不过"
    # 而真正说"没有故障"的结论要能过（用历史上的正确结论核对）
    noissue_right = (
        "全面看完了。结论是：集群目前没有故障。当前状态：健康。"
    )
    assert root_cause_ok_for(by_name["noissue"], noissue_right) is True


def test_根因门禁的尺寸不变():
    """护栏：不许为了让哪条好看去把 root_cause_all 删掉或选过松的词。

    例如 stress 必须同时要"chaos 词"和"CPU/压力词"——只留一个就等于没门禁。
    """
    by_name = {s.name: s for s in SCENARIOS}
    stress = by_name["stress"].root_cause_all
    assert any("chaos" in w.lower() for w in stress), "stress 少了 chaos 类词"
    assert any(w.lower() in ("cpu", "压力", "stress") for w in stress), \
        "stress 少了压力/CPU 类词"
    for name in ("netdelay", "netpart"):
        words = [w.lower() for w in by_name[name].root_cause_all]
        assert any("chaos" in w for w in words) and any("网络" in w for w in words), name
    # 空元组的那批不许被顺手清空成"没有门禁"（它们本来就没配，是允许的）；
    # 但上面四条必须非空，不能因为数字难看删掉。
    assert all(by_name[n].root_cause_all for n in
               ("stress", "noissue", "netdelay", "netpart"))


# ---------------------------------------------------------------------------
# 判据收紧之二：noissue 的前置检查（集群近期有扰动 → 跳过）
# ---------------------------------------------------------------------------


class _FakePod:
    def __init__(self, name: str, age_s: float, restarts: int = 0):
        import datetime as _dt

        self.metadata = SimpleNamespace(
            name=name,
            creation_timestamp=_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(seconds=age_s),
        )
        self.status = SimpleNamespace(
            container_statuses=[SimpleNamespace(restart_count=restarts)]
        )


class _FakeCore:
    def __init__(self, pods):
        self._pods = pods

    def list_namespaced_pod(self, ns, **kw):
        return SimpleNamespace(items=list(self._pods))


class _FakeQuietK8s:
    def __init__(self, pods, events=()):
        self.core = _FakeCore(pods)
        self._events = list(events)

    def list_events(self, ns, involved_name=""):
        return list(self._events)


def _fake_agent(pods, events=()):
    return SimpleNamespace(k8s=_FakeQuietK8s(pods, events))


def test_noissue_近期有扰动就跳过():
    """有扰动 → 返回非空原因（调用方据此把它标成 skipped）。

    这正是上一轮假 PASS 的根因：reset 删掉重建工作负载，但 Loki/Prometheus
    的历史没人清，"集群健康"这个前提根本不成立。
    """
    from omagent.evals import _precheck_quiet_cluster

    sc = next(s for s in SCENARIOS if s.name == "noissue")
    assert sc.precheck == "quiet_cluster"

    # 刚被重建出来的 Pod（reset 的产物）→ 必须拦
    agent = _fake_agent([_FakePod("api-gateway-abc", age_s=30)])
    reason = _precheck_quiet_cluster(agent, sc)
    assert reason, "新建 Pod 没被当成扰动"
    assert "新创建" in reason

    # 容器重启也算扰动
    agent = _fake_agent([_FakePod("api-gateway-abc", age_s=99999, restarts=1)])
    assert _precheck_quiet_cluster(agent, sc)


def test_noissue_安静时才放行():
    """窗口内没有新建 Pod、没有重启、没有扰动事件 → 返回 ""（可以真跑）。"""
    from omagent.evals import _precheck_quiet_cluster

    sc = next(s for s in SCENARIOS if s.name == "noissue")
    agent = _fake_agent([_FakePod("api-gateway-abc", age_s=24 * 3600)])
    assert _precheck_quiet_cluster(agent, sc) == ""


def test_noissue_查不动时_fail_closed_成跳过():
    """拿不到证据不能假装前提成立——那正是这条要防的假 PASS。"""
    from omagent.evals import _precheck_quiet_cluster

    class _Boom:
        class core:  # noqa: N801
            @staticmethod
            def list_namespaced_pod(ns, **kw):
                raise RuntimeError("api down")

    sc = next(s for s in SCENARIOS if s.name == "noissue")
    reason = _precheck_quiet_cluster(SimpleNamespace(k8s=_Boom()), sc)
    assert reason and "无法完成" in reason


def test_跳过不参与通过率分母():
    """skipped 既不算通过也不算不通过：不进分子、也不进分母。"""
    from omagent.evals import Outcome, summarise

    scs = [s for s in SCENARIOS if s.name in ("oom", "noissue")]
    ok = Outcome(scenario="oom", investigated=True, trap_avoided=True, action_ok=True)
    skip = Outcome(scenario="noissue", skipped=True, skip_reason="有扰动")
    rep = summarise(scs, [ok, skip])
    assert rep["total"] == 2 and rep["scored"] == 1 and rep["skipped"] == 1
    assert rep["passed"] == 1
    assert rep["pass_rate"] == 1.0, "跳过的场景被算进了分母"
    assert rep["skip_reasons"]["noissue"] == "有扰动"


# ---------------------------------------------------------------------------
# 判据收紧之三：provider 调用必须进报告
# ---------------------------------------------------------------------------


def test_provider_调用计入_read_calls(monkeypatch):
    """``evaluate_one`` 统计只读工具必须走 ``agent.spec_of``，不能再用静态表。

    静态表 ``READONLY_TOOLS`` 看不见 provider——上一轮真实跑过的数据源调用
    一次都没进报告，"查证"那一列是残缺的。
    """
    from types import SimpleNamespace as NS

    from omagent import evals
    from omagent.tools import ToolSpec

    provider_spec = ToolSpec(name="query_metrics", summary="查指标", schema={},
                             mutating=False)
    builtin_spec = ToolSpec(name="get_pods", summary="查 Pod", schema={},
                            mutating=False)

    class _Providers:
        def spec(self, name):
            return provider_spec if name == "query_metrics" else None

    agent = NS(spec_of=lambda t: {"get_pods": builtin_spec,
                                  "query_metrics": provider_spec}.get(t),
               providers=_Providers())

    events = [
        NS(type="tool", data={"tool": "get_pods"}),
        NS(type="tool", data={"tool": "query_metrics"}),
        NS(type="tool", data={"tool": "query_metrics"}),
        NS(type="assistant", data={"text": "结论"}),
    ]

    class _Sess:
        def __init__(self, *a, **k):
            self.events = events
            self.status = "idle"
            self.usage = {"total_tokens": 7}
            self._thread = None

        def send(self, q):
            pass

    monkeypatch.setattr("omagent.session.ChatSession", _Sess)
    monkeypatch.setattr(evals, "_run_fault", lambda sc: None)

    sc = next(s for s in SCENARIOS if s.name == "netdelay")
    out = evals.evaluate_one(sc, agent_factory=lambda: agent,
                             llm_factory=lambda: None, settle=0.0)
    assert out.read_calls == 3, out.read_calls
    assert out.provider_calls == 2, out.provider_calls
    assert out.provider_tools == ["query_metrics", "query_metrics"]
    # investigated 的语义不能被改坏：有只读调用就算查过
    assert out.investigated is True


# ---------------------------------------------------------------------------
# 判据收紧之四：dependency 的题干不许点名被救的那一侧
# ---------------------------------------------------------------------------


def test_dependency_题干不点名_postgres_primary():
    """``dependency`` 的 target 是**被缩到 0 的依赖**（postgres-primary），
    而 expect 要救的正是它。通用题干「postgres-primary 好像出问题了」等于
    先把"坏的是依赖那侧"这半个答案说了。

    正解：从**报错的调用方**（faults.sh 的 pick_observer 选的 order-service）
    视角问，只给症状，不点名依赖。
    """
    sc = next(s for s in SCENARIOS if s.name == "dependency")
    q = sc.question
    assert sc.ask, "dependency 必须自定义 ask"
    assert "postgres" not in q.lower(), f"题干泄题了：{q!r}"
    assert sc.target.lower() not in q.lower()
    # 从报错的那一侧问：题干要出现调用方（= 真正刷 connection refused 的服务）
    assert "order-service" in q


def test_noissue_没有靠题干泄题去绕开历史():
    """护栏：不许用"改题干提示它历史里有测试活动"绕过前置检查——那是泄题。"""
    sc = next(s for s in SCENARIOS if s.name == "noissue")
    q = sc.question.lower()
    for leak in ("测试", "评测", "上一轮", "注入", "历史", "chaos", "prometheus", "loki"):
        assert leak.lower() not in q, f"noissue 题干泄题：{leak!r}"
