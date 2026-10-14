"""容量与成本的回归测试。

这个模块有两条**诚实性**上的硬要求，比功能本身更容易被无声破坏：

1. **没有价格表就不许算钱。** 一旦哪天有人图省事加了"默认单价"，
   运维就会拿着编出来的数字去做预算决策。所以这里钉死"没有就返回 None"。
2. **原样照抄示例价格必须被警告。** 第一版这个判断就写错了（条件里要求
   "没有 node_overrides"，而示例本来就有），警告永远不触发——等于没写。

另外还有两个数量口径的坑，也都在这里钉住：
   · 节点余量必须用**全集群** Pod 算（用过命名空间过滤，导致"10 个 Pod
     却只预留了 0.5 核"这种自相矛盾的输出）
   · PDB 的 minAvailable 是 IntOrString，`"50%"` **不能被 int() 吃掉**
"""

from __future__ import annotations

import json

import pytest

from omagent import capacity
from omagent.k8s import K8sClient


# ---------------------------------------------------------------------------
# 价格表：没有就不许算
# ---------------------------------------------------------------------------


def test_没有价格表就不返回任何单价(tmp_path):
    """**这条是整个成本功能的诚实底线。**"""
    missing = tmp_path / "nope.json"
    assert capacity.load_pricing(missing) is None


def test_没有价格表时的说明里包含怎么配(tmp_path):
    note = capacity._pricing_missing_note()
    assert "不算钱" in note
    assert "pricing.example.json" in note
    # 必须说清为什么不内置默认价
    assert "编" in note


def test_价格表能读进来(tmp_path):
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({
        "currency": "USD", "cpu_per_core": 0.05, "memory_per_gib": 0.01,
        "storage_per_gib": 0.001,
        "node_overrides": {"gpu-node": {"cpu_per_core": 1.5}},
    }), encoding="utf-8")
    p = capacity.load_pricing(f)
    assert p is not None
    assert p.currency == "USD"
    assert p.rates_for("") == (0.05, 0.01)
    assert p.rates_for("gpu-node") == (1.5, 0.01), "节点覆盖没生效"
    assert p.rates_for("other-node") == (0.05, 0.01)


def test_价格表坏了要明确报错而不是静默用零(tmp_path):
    f = tmp_path / "pricing.json"
    f.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(Exception):
        capacity.load_pricing(f)


def test_照抄示例价格会被识别出来():
    """示例表在仓库里，照抄一份就该被警告——否则那个数字会被当真。"""
    real = capacity.PRICING_EXAMPLE
    if not real.exists():
        pytest.skip("仓库里没有 pricing.example.json")
    p = capacity.load_pricing(real)
    assert p is not None
    assert capacity._looks_like_example(p) is True


def test_换成自己的单价就不该再警告(tmp_path):
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"cpu_per_core": 9.99, "memory_per_gib": 8.88}),
                 encoding="utf-8")
    p = capacity.load_pricing(f)
    assert capacity._looks_like_example(p) is False


# ---------------------------------------------------------------------------
# 工作负载名的还原
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("owner,want", [
    ("api-gateway-7d9f8c6b5", "api-gateway"),
    ("cart-service-c74b88b68", "cart-service"),
    ("postgres-primary-0", "postgres-primary-0"),   # StatefulSet，不带 hash
    ("web-frontend-78754b9dc8", "web-frontend"),
    ("nodots", "nodots"),
    ("a-b", "a-b"),                                  # 尾巴太短，不当 hash
])
def test_工作负载名要去掉replicaset的hash(owner, want):
    assert capacity._workload_of(owner) == want


# ---------------------------------------------------------------------------
# 改配额
# ---------------------------------------------------------------------------


class _Core:
    def __init__(self) -> None:
        self.patches: list[dict] = []

    def patch_namespaced_resource_quota(self, name, ns, body, dry_run=None):
        self.patches.append({"name": name, "ns": ns, "body": body, "dry_run": dry_run})


def _bare():
    k = object.__new__(K8sClient)
    k.core = _Core()
    return k


def test_改配额_空参数被拒():
    from omagent.k8s import ActionRejected

    k = _bare()
    with pytest.raises(ActionRejected, match="不能同时为空"):
        capacity.patch_resourcequota(k, "demo", "q", {}, "All")
    assert k.core.patches == [], "被拒的请求不该真的发出去"


def test_改配额_纯新增走merge_patch且传了dry_run():
    k = _bare()
    capacity.patch_resourcequota(
        k, "demo", "q", {"hard": {"requests.cpu": 8}}, "All")
    call = k.core.patches[0]
    assert call["dry_run"] == "All", "dry-run 没传下去，卡片上的干跑就是假的"
    assert call["body"] == {"spec": {"hard": {"requests.cpu": "8"}}}
    assert call["ns"] == "demo" and call["name"] == "q"


def test_改配额_删除键必须走json_patch():
    """merge patch 删不掉 map 里已有的键——有 remove 就必须换 JSON patch。"""
    k = _bare()
    capacity.patch_resourcequota(
        k, "demo", "q", {"remove": ["limits.memory"]}, "All")
    body = k.core.patches[0]["body"]
    assert isinstance(body, list), "有 remove 时必须是 JSON patch"
    assert body[0]["op"] == "remove"
    assert body[0]["path"] == "/spec/hard/limits.memory"


def test_改配额_键里的斜杠要按json_pointer转义():
    assert capacity._ptr("requests.cpu") == "requests.cpu"
    assert capacity._ptr("a/b") == "a~1b"
    assert capacity._ptr("a~b") == "a~0b"


# ---------------------------------------------------------------------------
# 改 PDB
# ---------------------------------------------------------------------------


class _Policy:
    def __init__(self) -> None:
        self.patches: list[dict] = []

    def patch_namespaced_pod_disruption_budget(self, name, ns, body, dry_run=None):
        self.patches.append({"name": name, "ns": ns, "body": body, "dry_run": dry_run})


def _bare_pdb():
    k = object.__new__(K8sClient)
    k.policy_api = _Policy()
    return k


def test_改pdb_空参数被拒():
    from omagent.k8s import ActionRejected

    k = _bare_pdb()
    with pytest.raises(ActionRejected, match="至少要给"):
        capacity.patch_pdb(k, "demo", "p", {}, "All")
    assert k.policy_api.patches == []


def test_改pdb_两个字段互斥():
    """minAvailable 和 maxUnavailable 同时给会被 API 拒绝——不如在本地就拦掉，
    省得操作员对着卡片点了批准才看到报错。"""
    from omagent.k8s import ActionRejected

    k = _bare_pdb()
    with pytest.raises(ActionRejected, match="互斥"):
        capacity.patch_pdb(k, "demo", "p", {"min_available": 1, "max_unavailable": 1}, "All")
    assert k.policy_api.patches == []


def test_改pdb_百分比必须原样保留():
    """IntOrString：`\"50%\"` 不能被 int() 吃掉。吃掉会变成 0 或报错，
    而 PDB 是**唯一**能拦住节点排空打挂服务的东西。"""
    k = _bare_pdb()
    capacity.patch_pdb(k, "demo", "p", {"min_available": "50%"}, "All")
    body = k.policy_api.patches[0]["body"]
    assert body[0]["value"] == "50%"
    assert isinstance(body[0]["value"], str)


def test_改pdb_数字要转成int():
    k = _bare_pdb()
    capacity.patch_pdb(k, "demo", "p", {"max_unavailable": "2"}, "All")
    assert k.policy_api.patches[0]["body"][0]["value"] == 2


def test_改pdb_既不是数字也不是百分比要报错():
    from omagent.k8s import ActionRejected

    k = _bare_pdb()
    with pytest.raises(ActionRejected, match="既不是数字也不是百分比"):
        capacity.patch_pdb(k, "demo", "p", {"min_available": "一半"}, "All")
    assert k.policy_api.patches == []


def test_改pdb_传了dry_run():
    k = _bare_pdb()
    capacity.patch_pdb(k, "demo", "p", {"min_available": 1}, "All")
    assert k.policy_api.patches[0]["dry_run"] == "All"


# ---------------------------------------------------------------------------
# 成本：不指定命名空间时也要能算闲置
# ---------------------------------------------------------------------------


def test_不指定命名空间时闲置成本也要算得出来(tmp_path, monkeypatch):
    """踩过：用量是按命名空间拉的，不指定命名空间时直接给了空 dict，
    于是集群级的「闲置成本」永远算不出来——而那正是这个工具最值钱的输出。"""
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"cpu_per_core": 1.0, "memory_per_gib": 0.0}),
                 encoding="utf-8")
    real = capacity.load_pricing          # 先抓住原函数，否则补丁里调自己 = 无限递归
    monkeypatch.setattr(capacity, "load_pricing", lambda *a, **k: real(f))

    asked: list[str] = []

    class _K:
        def metrics_available(self):
            return True

        def pod_usage(self, ns):
            asked.append(ns)
            return {}

        def list_nodes(self):
            return []

    monkeypatch.setattr(capacity, "pod_requests", lambda k, ns=None: [
        {"pod": "p1", "namespace": "demo", "node": "n1", "owner": "x-abc12", "cpu": 1.0, "mem": 0.0},
        {"pod": "p2", "namespace": "staging", "node": "n1", "owner": "y-abc12", "cpu": 1.0, "mem": 0.0},
    ])
    out = capacity.describe_cost(_K(), "")
    assert asked == ["demo", "staging"], f"没有逐个命名空间拉用量：{asked}"
    assert "闲置成本" in out
