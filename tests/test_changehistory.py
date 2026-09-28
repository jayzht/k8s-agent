"""变更历史：把「最近改了什么」算出来。

加这个是因为评测跑了 5 轮，最稳定的三个失败（probe/crash/cordon）**本质是同一个
盲区**：模型不会把"当前症状"和"最近一次变更"联系起来。原来的工具只能看到此刻，
要发现"十分钟前探针端口被改了"得靠模型自己做一次 diff——它做不到。
"""

from __future__ import annotations

from omagent.changehistory import MAX_DIFFS, diff


def test_标量变化():
    assert diff({"a": 1}, {"a": 2}) == [("a", "1", "2")]


def test_新增字段显示为不存在而不是空():
    """nodeSelector 被**新增**是 cordon 场景的根因，必须看得出来是"从无到有"。"""
    out = diff({}, {"nodeSelector": {"node-pool": "batch"}})
    # 用「<不存在>」而不是「（无）」：要能区分"字段不存在"和"存在但为空"
    assert out == [("nodeSelector", "<不存在>", "{'node-pool': 'batch'}")]


def test_删掉的字段也要报():
    assert diff({"a": 1, "b": 2}, {"a": 1}) == [("b", "2", "<不存在>")]


def test_探针端口这种嵌套变化路径要写全():
    """probe 场景的根因就长这样，路径写全了模型才知道改哪。"""
    out = diff({"livenessProbe": {"httpGet": {"port": 8080}}},
               {"livenessProbe": {"httpGet": {"port": 9090}}})
    assert out == [("livenessProbe.httpGet.port", "8080", "9090")]


def test_容器按名字配对而不是下标():
    """容器顺序变了不该显示成"整个容器被换掉"。"""
    a = {"containers": [{"name": "app", "image": "x:1"},
                        {"name": "sidecar", "image": "s:1"}]}
    b = {"containers": [{"name": "sidecar", "image": "s:1"},
                        {"name": "app", "image": "x:2"}]}
    assert diff(a, b) == [("containers[app].image", "x:1", "x:2")]


def test_没有差异返回空():
    same = {"containers": [{"name": "app", "image": "x:1"}]}
    assert diff(same, same) == []


def test_差异条数有上限():
    """变更很大的发布会产生几十条，全塞进上下文会把有用的挤掉。"""
    a = {f"k{i}": 1 for i in range(50)}
    b = {f"k{i}": 2 for i in range(50)}
    assert len(diff(a, b)) <= MAX_DIFFS


def test_列表长度变化时报长度差():
    assert diff({"args": ["a"]}, {"args": ["a", "b"]}) == [("args", "1 项", "2 项")]
