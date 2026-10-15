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
