"""容量与成本。

这个模块补的是两块以前**完全没有**的能力：

  容量 —— 以前只能读 ResourceQuota 和节点标签，**不能改配额，也不会算余量**。
          运维问"新服务还部署得下吗""该不该加节点"，系统答不了。
  成本 —— 以前完全没有。而 FinOps 里最值钱的那个问题偏偏很简单：
          **有多少钱花在"预留了但没用"的资源上。**

为什么单独开一个模块，而不是继续往 ``k8s.py`` 里塞：那个文件已经两千多行，
而容量/成本是**自成一套的计算**（节点可分配 → Pod requests → 实际用量 → 单价
→ 余量与花费），放在一起能一眼看出这条链。工具定义仍在 ``tools.py``，
分发仍在 ``k8s.py``，这里只放实现。

⚠️ 关于成本的**诚实边界**（这一段是设计的一部分，不是免责声明）：

本模块**不调用任何云厂商计费 API，也不内置任何价格**。单价必须由运维自己
填在 ``var/pricing.json`` 里（模板见仓库根目录的 ``pricing.example.json``）。

为什么不内置一组"看起来像真的"价格：运维会拿这个数字去做容量和预算决策，
而那个数字是我编的。**宁可这个工具在没有价格表时什么都不算、只说清楚怎么配**，
也不要给一个会被人当真的假账。同理，如果检测到用的还是示例价格，
输出里会显式警告。

计价口径是 **requests（预留量）**，不是实际用量——预留才是真正付钱的东西。
实际用量只用来算"预留了但没用"的那部分，那才是可以省下来的钱。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ROOT
from .k8s import ActionRejected, K8sClient, parse_quantity

PRICING_PATH = ROOT / "var" / "pricing.json"
PRICING_EXAMPLE = ROOT / "pricing.example.json"

GIB = 1024.0 ** 3
HOURS_PER_MONTH = 730.0


# ---------------------------------------------------------------------------
# 价格表
# ---------------------------------------------------------------------------


@dataclass
class Pricing:
    """一份单价表。所有单价都是"每单位每小时"。"""

    currency: str = "CNY"
    cpu_per_core: float = 0.0
    memory_per_gib: float = 0.0
    storage_per_gib: float = 0.0
    node_overrides: dict[str, dict[str, float]] = field(default_factory=dict)
    source: str = ""
    is_example: bool = False

    def rates_for(self, node: str = "") -> tuple[float, float]:
        """返回 (每核单价, 每 GiB 单价)。节点有覆盖就用覆盖的。"""
        cpu, mem = self.cpu_per_core, self.memory_per_gib
        ov = self.node_overrides.get(node) or {}
        if "cpu_per_core" in ov:
            cpu = float(ov["cpu_per_core"])
        if "memory_per_gib" in ov:
            mem = float(ov["memory_per_gib"])
        return cpu, mem

    def cost(self, cpu_cores: float, mem_bytes: float, node: str = "") -> float:
        cpu, mem = self.rates_for(node)
        return cpu_cores * cpu + (mem_bytes / GIB) * mem


def load_pricing(path: Path | None = None) -> Pricing | None:
    """读价格表。**没有就返回 None**——不编默认价。

    只有 ``var/pricing.json`` 存在时才返回 Pricing。仓库里的
    ``pricing.example.json`` 只用于在缺失时**给用户看格式**，
    绝不拿它的数字去算钱（那正是"编一组看起来像真的价格"）。
    """
    p = Path(path) if path else PRICING_PATH
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionRejected(f"价格表 {p} 读不了：{exc}") from exc
    if not isinstance(raw, dict):
        raise ActionRejected(f"价格表 {p} 应该是一个 JSON 对象")
    return Pricing(
        currency=str(raw.get("currency") or "CNY"),
        cpu_per_core=float(raw.get("cpu_per_core") or 0.0),
        memory_per_gib=float(raw.get("memory_per_gib") or 0.0),
        storage_per_gib=float(raw.get("storage_per_gib") or 0.0),
        node_overrides={
            str(k): {kk: float(vv) for kk, vv in (v or {}).items()}
            for k, v in (raw.get("node_overrides") or {}).items()
        },
        source=str(p),
    )


def _pricing_missing_note() -> str:
    shape = ""
    if PRICING_EXAMPLE.exists():
        try:
            shape = PRICING_EXAMPLE.read_text(encoding="utf-8").strip()
        except OSError:
            shape = ""
    lines = [
        "没有价格表，所以**不算钱**——本工具不调用云厂商计费 API，也不内置价格。",
        f"要启用：把仓库根目录的 `pricing.example.json` 复制成 `{PRICING_PATH}`，",
        "然后把里面的数字换成你自己账单上的等效单价（每核每小时、每 GiB 每小时）。",
        "",
        "为什么不内置一组默认价：这个数字会被拿去做容量和预算决策，",
        "编出来的价格比没有价格更糟。",
    ]
    if shape:
        lines += ["", "价格表的格式长这样：", shape[:900]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 从集群里取数
# ---------------------------------------------------------------------------


def pod_requests(k8s: K8sClient, namespace: str | None = None) -> list[dict[str, Any]]:
    """每个 Pod 的 requests 汇总，带上它的节点和工作负载归属。

    为什么要单独走一次 Pod 列表而不是复用 ``list_pods``：``PodInfo`` 里
    只有 limit 和用量，**没有 requests**。而 requests 才是调度和计费的口径。
    """
    try:
        if namespace:
            items = k8s.core.list_namespaced_pod(namespace).items
        else:
            items = k8s.core.list_pod_for_all_namespaces().items
    except Exception:  # noqa: BLE001
        return []

    out: list[dict[str, Any]] = []
    for p in items:
        if getattr(p.status, "phase", "") in ("Succeeded", "Failed"):
            continue
        cpu = mem = 0.0
        for c in (p.spec.containers or []):
            res = getattr(c, "resources", None)
            req = (getattr(res, "requests", None) or {}) if res else {}
            cpu += parse_quantity(req.get("cpu"))
            mem += parse_quantity(req.get("memory"))
        owner = ""
        for ref in (p.metadata.owner_references or []):
            if ref.kind in ("ReplicaSet", "StatefulSet", "DaemonSet", "Job"):
                owner = ref.name
                break
            if ref.kind == "Node":
                owner = ref.name
        out.append({
            "pod": p.metadata.name,
            "namespace": p.metadata.namespace,
            "node": getattr(p.spec, "node_name", "") or "",
            "owner": owner,
            "cpu": cpu,
            "mem": mem,
        })
    return out


def _workload_of(owner: str) -> str:
    """ReplicaSet 名去掉结尾的 pod-template hash，还原成工作负载名。

    ``api-gateway-7d9f8c6b5`` → ``api-gateway``。收尾那段一定是 5~10 位的
    小写字母数字（K8s 生成的 hash），不是的话原样返回。
    """
    if "-" not in owner:
        return owner
    head, _, tail = owner.rpartition("-")
    if head and 5 <= len(tail) <= 10 and tail.isalnum() and tail.islower():
        return head
    return owner


def _fmt_cpu(cores: float) -> str:
    return f"{cores:.2f} 核"


def _fmt_mem(nbytes: float) -> str:
    if nbytes >= GIB:
        return f"{nbytes / GIB:.1f} GiB"
    return f"{nbytes / (1024 ** 2):.0f} MiB"


# ---------------------------------------------------------------------------
# 容量：还塞得下吗 / 该加节点吗
# ---------------------------------------------------------------------------


def describe_capacity(k8s: K8sClient, namespace: str = "", top: int = 5) -> str:
    """集群余量：按节点看"预留了多少、实际用了多少、还能塞多少"。

    这个工具回答的是运维最常问的两个问题：
      · 新服务还部署得下吗？（看**预留**，因为调度是按 requests 算的）
      · 该不该加节点/该不该缩水？（看**预留** 与**实际用量**的差）
    """
    try:
        nodes = k8s.list_nodes()
    except Exception as exc:  # noqa: BLE001
        raise ActionRejected(f"读节点失败：{exc}") from exc
    if not nodes:
        return "读不到节点信息。"

    usage = k8s.node_usage() if k8s.metrics_available() else {}

    # ⚠️ 节点余量**必须用全集群的 Pod** 来算，不能用某个命名空间的。
    # 踩过：一开始用传入的 namespace 过滤，结果输出成"10 个 Pod，已预留 0.50 核"
    # ——Pod 数是全集群的、预留量只是一个命名空间的，两个数字相互矛盾，
    # 而"这个节点还塞得下吗"正是靠预留量回答的。宁可不给结论也不能给错数。
    all_reqs = pod_requests(k8s, None)
    reqs = pod_requests(k8s, namespace) if namespace else all_reqs

    per_node: dict[str, dict[str, float]] = {
        n["name"]: {"cpu_cap": n["cpu_allocatable"], "mem_cap": n["mem_allocatable"],
                    "cpu_req": 0.0, "mem_req": 0.0, "pods": 0.0}
        for n in nodes
    }
    for r in all_reqs:
        slot = per_node.get(r["node"])
        if slot is None:
            continue  # 落在集群外的（比如已删节点上的僵尸 Pod）
        slot["cpu_req"] += r["cpu"]
        slot["mem_req"] += r["mem"]
        slot["pods"] += 1

    lines = ["**节点余量**（调度按 requests 算，所以看「已预留」那一列）", ""]
    hot: list[str] = []
    for n in nodes:
        name = n["name"]
        s = per_node[name]
        cap_c, cap_m = s["cpu_cap"], s["mem_cap"]
        rc = (s["cpu_req"] / cap_c * 100) if cap_c else 0.0
        rm = (s["mem_req"] / cap_m * 100) if cap_m else 0.0
        head = f"{name}{'（已 cordon）' if n.get('unschedulable') else ''}"
        line = (f"  {head}: 可分配 {_fmt_cpu(cap_c)} / {_fmt_mem(cap_m)}"
                f" ｜ 已预留 {_fmt_cpu(s['cpu_req'])} ({rc:.0f}%)"
                f" / {_fmt_mem(s['mem_req'])} ({rm:.0f}%)"
                f" ｜ {int(s['pods'])} 个 Pod")
        u = usage.get(name)
        if u:
            uc = (u["cpu_cores"] / cap_c * 100) if cap_c else 0.0
            um = (u["mem_bytes"] / cap_m * 100) if cap_m else 0.0
            line += (f"\n      实际使用 {_fmt_cpu(u['cpu_cores'])} ({uc:.0f}%)"
                     f" / {_fmt_mem(u['mem_bytes'])} ({um:.0f}%)")
        lines.append(line)
        if rc >= 80 or rm >= 80:
            hot.append(f"{name}（预留 {max(rc, rm):.0f}%）")

    tc = sum(s["cpu_cap"] for s in per_node.values())
    tm = sum(s["mem_cap"] for s in per_node.values())
    rc_all = sum(s["cpu_req"] for s in per_node.values())
    rm_all = sum(s["mem_req"] for s in per_node.values())
    lines += [
        "",
        f"**集群合计**：可分配 {_fmt_cpu(tc)} / {_fmt_mem(tm)}；"
        f"已预留 {_fmt_cpu(rc_all)} ({(rc_all / tc * 100) if tc else 0:.0f}%) / "
        f"{_fmt_mem(rm_all)} ({(rm_all / tm * 100) if tm else 0:.0f}%)",
        f"→ 按**预留**口径还能调度约 {_fmt_cpu(max(tc - rc_all, 0))} / "
        f"{_fmt_mem(max(tm - rm_all, 0))}（这是上限；实际还受单 Pod 最大规格、"
        f"反亲和、污点影响）",
    ]
    if not usage:
        lines.append("（拿不到实际用量：metrics-server 没装或还没采集，"
                     "所以只能看预留、看不了'预留了但没用'）")
    if hot:
        lines.append(f"⚠️ 预留率 ≥80% 的节点：{'、'.join(hot)}——"
                     f"新 Pod 可能调度不上去，该考虑扩容或把 requests 调小")

    # 配额：把"命名空间级别的天花板"也摆出来
    ns = namespace or "demo"
    try:
        quotas = k8s.list_resourcequotas(ns)
    except Exception:  # noqa: BLE001
        quotas = []
    if quotas:
        lines += ["", f"**命名空间 {ns} 的配额**"]
        for q in quotas:
            for k, hard in sorted(q["hard"].items()):
                used = q["used"].get(k, "-")
                flag = "  ⚠️ 已用满" if k in q["exhausted"] else ""
                lines.append(f"  {q['name']} {k}: 已用 {used} / 上限 {hard}{flag}")

    # 谁占得最多
    if reqs:
        agg: dict[str, dict[str, float]] = {}
        for r in reqs:
            key = _workload_of(r["owner"]) or r["pod"]
            a = agg.setdefault(key, {"cpu": 0.0, "mem": 0.0, "n": 0.0})
            a["cpu"] += r["cpu"]
            a["mem"] += r["mem"]
            a["n"] += 1
        top_items = sorted(agg.items(), key=lambda kv: kv[1]["cpu"], reverse=True)[:top]
        lines += ["", f"**预留最多的**（命名空间 {ns}）"]
        for name, a in top_items:
            lines.append(f"  {name}: {_fmt_cpu(a['cpu'])} / {_fmt_mem(a['mem'])}"
                         f"（{int(a['n'])} 个 Pod）")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 成本：钱花在哪，以及哪部分是可以省的
# ---------------------------------------------------------------------------


def describe_cost(k8s: K8sClient, namespace: str = "", top: int = 5) -> str:
    """按价格表算钱：预留成本、按命名空间/工作负载的拆分、闲置成本。

    **闲置成本是这个工具真正的价值**。运维能从别处看到"谁用了多少"，
    但很难一眼看出"我为一堆根本没跑的规格付了多少钱"。
    """
    pricing = load_pricing()
    if pricing is None:
        return _pricing_missing_note()

    reqs = pod_requests(k8s, namespace or None)
    if not reqs:
        return f"没有可统计的 Pod（命名空间 {namespace or '全部'}）。"

    # 用量是按命名空间拉的（metrics API 就是这么分的）。不指定命名空间时
    # 要把涉及的命名空间**逐个**拉一遍——否则集群级的「闲置成本」永远算不出来，
    # 而那个数恰恰是这个工具最值钱的输出。
    usage: dict[str, dict[str, float]] = {}
    if k8s.metrics_available():
        nss = [namespace] if namespace else sorted({r["namespace"] for r in reqs})
        for nsn in nss:
            try:
                usage.update(k8s.pod_usage(nsn))
            except Exception:  # noqa: BLE001
                continue

    # 节点单价覆盖要先知道 Pod 在哪个节点上
    by_ns: dict[str, dict[str, float]] = {}
    by_wl: dict[str, dict[str, float]] = {}
    total = 0.0
    idle = 0.0
    idle_known = bool(usage)

    for r in reqs:
        c = pricing.cost(r["cpu"], r["mem"], r["node"])
        total += c
        n = by_ns.setdefault(r["namespace"], {"cost": 0.0, "cpu": 0.0, "mem": 0.0})
        n["cost"] += c
        n["cpu"] += r["cpu"]
        n["mem"] += r["mem"]
        wl = _workload_of(r["owner"]) or r["pod"]
        w = by_wl.setdefault(wl, {"cost": 0.0, "cpu": 0.0, "mem": 0.0, "n": 0.0})
        w["cost"] += c
        w["cpu"] += r["cpu"]
        w["mem"] += r["mem"]
        w["n"] += 1
        if idle_known:
            u = usage.get(r["pod"])
            if u:
                idle += pricing.cost(
                    max(r["cpu"] - u["cpu_cores"], 0.0),
                    max(r["mem"] - u["mem_bytes"], 0.0),
                    r["node"],
                )

    cur = pricing.currency
    lines = [
        f"**计价口径：requests（预留量）**，单价来自 `{pricing.source}`"
        f"（{cur} / 每小时）。这是**你自己的单价**，不是云厂商账单。",
    ]
    if _looks_like_example(pricing):
        lines.append("⚠️ **检测到用的还是仓库里的示例价格**——这个数字不能拿去做"
                     "预算决策，请把 `var/pricing.json` 换成你自己账单上的单价。")
    lines += [
        "",
        f"**总计**：{total:.4f} {cur}/小时 ≈ **{total * HOURS_PER_MONTH:,.2f} {cur}/月**"
        f"（按 {HOURS_PER_MONTH:.0f} 小时/月）",
    ]

    lines += ["", "**按命名空间**"]
    for ns_name, a in sorted(by_ns.items(), key=lambda kv: kv[1]["cost"], reverse=True):
        pct = (a["cost"] / total * 100) if total else 0
        lines.append(f"  {ns_name}: {a['cost']:.4f} {cur}/小时"
                     f" ≈ {a['cost'] * HOURS_PER_MONTH:,.2f}/月 ({pct:.0f}%)"
                     f" ｜ {_fmt_cpu(a['cpu'])} / {_fmt_mem(a['mem'])}")

    lines += ["", f"**最贵的 {top} 个工作负载**"]
    for wl, a in sorted(by_wl.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:top]:
        pct = (a["cost"] / total * 100) if total else 0
        lines.append(f"  {wl}: {a['cost'] * HOURS_PER_MONTH:,.2f} {cur}/月 ({pct:.0f}%)"
                     f" ｜ 预留 {_fmt_cpu(a['cpu'])} / {_fmt_mem(a['mem'])}"
                     f"（{int(a['n'])} 个 Pod）")

    lines += [""]
    if idle_known:
        pct = (idle / total * 100) if total else 0
        lines += [
            f"**闲置成本（预留了但没用）**：约 {idle:.4f} {cur}/小时"
            f" ≈ **{idle * HOURS_PER_MONTH:,.2f} {cur}/月**（占 {pct:.0f}%）",
            "→ 这部分是**可以省的**：把相应工作负载的 requests 调到接近实际用量。",
            "⚠️ 但不要只为了省钱把 requests 压到贴着用量——requests 同时是调度"
            "和驱逐的依据，压太紧会在负载高峰时被 OOMKill 或调度不上去。"
            "留多少余量是业务判断，不是这个工具能替你决定的。",
        ]
    else:
        lines.append(
            "算不出**闲置成本**：拿不到实际用量（metrics-server 没装或还没采集）。"
            "只有预留量的时候，「该花多少」和「实际用了多少」分不开——"
            "而后者才是能省下来的钱。")

    return "\n".join(lines)


def _read_keys(path: str) -> set[str]:
    try:
        return set(json.loads(Path(path).read_text(encoding="utf-8")).keys())
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def _example_shape() -> dict[str, Any]:
    """读示例价格表，只用来判断「用户是不是原样照抄了示例」。

    ⚠️ 第一版这里判错过：条件是「单价相同 **且** 没有 node_overrides」，
    而示例里本来就带 node_overrides，所以照抄之后那个条件恒为假——
    警告永远不触发，等于没写。判断"是不是示例"要整体比，不能只比一部分。
    """
    if not PRICING_EXAMPLE.exists():
        return {}
    try:
        return json.loads(PRICING_EXAMPLE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _looks_like_example(p: Pricing) -> bool:
    """单价和示例一模一样 → 认为用户在拿示例价当真。

    两个判据取或：单价全等，或者文件里还留着示例的 `_README` 说明键
    （后者更宽松，能兜住"改了单价但没删说明"的情况）。
    """
    raw = _example_shape()
    if not raw:
        return False
    try:
        same = (abs(p.cpu_per_core - float(raw.get("cpu_per_core") or 0.0)) < 1e-12
                and abs(p.memory_per_gib - float(raw.get("memory_per_gib") or 0.0)) < 1e-12
                and abs(p.storage_per_gib - float(raw.get("storage_per_gib") or 0.0)) < 1e-12
                and p.node_overrides == {
                    str(k): {kk: float(vv) for kk, vv in (v or {}).items()}
                    for k, v in (raw.get("node_overrides") or {}).items()
                })
    except (TypeError, ValueError):
        same = False
    return same or ("_README" in raw and p.source.endswith("pricing.json")
                    and "_README" in _read_keys(p.source))


# ---------------------------------------------------------------------------
# 写操作：改配额 / 改 PDB
# ---------------------------------------------------------------------------


def _ptr(key: str) -> str:
    """把 map 的键转成 JSON Pointer 里合法的段。

    JSON Pointer (RFC 6901) 规定 `~` 写成 `~0`、`/` 写成 `~1`。
    配额项的键全是 `requests.cpu` / `limits.memory` 这种**带点**的，
    不含 `/`，但转义还是照规矩做——万一遇到自定义资源名带斜杠就炸了。
    """
    return key.replace("~", "~0").replace("/", "~1")


def patch_resourcequota(k8s: K8sClient, ns: str, name: str,
                        params: dict[str, Any], dry: str | None) -> str:
    """改 ResourceQuota 的 hard 上限。

    ⚠️ 两个容易出事的点，都写在这里免得有人踩：
    1. **把上限调到低于当前用量不会驱逐任何东西**，只会让新建 Pod 全部失败。
       运维以为"调小了就能回收"，实际是"接下来什么都起不来"。
       所以这里改完会**回读一次**，把"哪些项已经超了"直接报出来。
    2. 加一个**没有对应默认限制的项**（比如只加 `requests.ephemeral-storage`
       而没配 LimitRange）会让**所有**新 Pod 被拒——这个必须在卡片上说清楚。
    """
    hard = params.get("hard") or {}
    remove = params.get("remove") or []
    if not isinstance(hard, dict):
        raise ActionRejected("hard 必须是一个对象，比如 {\"requests.cpu\": \"8\"}")
    if not hard and not remove:
        raise ActionRejected("hard 和 remove 不能同时为空——那等于什么都没改")

    if remove:
        # hard 是个 map，merge patch **删不掉已有的键**，所以有 remove 时必须走
        # JSON patch（这和 patch_node_selector 踩的是同一个坑）。
        # ⚠️ JSON Pointer 里的 `/` 必须转义成 `~1`、`~` 转义成 `~0`，
        # 否则 `requests.cpu` 会被当成两级路径，patch 直接失败。
        body: list[dict[str, Any]] = []
        for key in remove:
            body.append({"op": "remove", "path": "/spec/hard/" + _ptr(key)})
        for key, val in hard.items():
            body.append({"op": "add", "path": "/spec/hard/" + _ptr(key), "value": str(val)})
    else:
        body = {"spec": {"hard": {k: str(v) for k, v in hard.items()}}}

    try:
        k8s.core.patch_namespaced_resource_quota(name, ns, body, dry_run=dry)
    except Exception as exc:  # noqa: BLE001
        raise ActionRejected(f"改配额被拒绝: {getattr(exc, 'reason', exc)}") from exc

    out = f"已更新 ResourceQuota/{name}：{'+'.join(list(hard) + [f'-{k}' for k in remove])}"

    if dry is None:
        # 回读：调小之后可能立刻就有项超了，这必须让操作员当场知道
        over: list[str] = []
        try:
            for q in k8s.list_resourcequotas(ns):
                if q["name"] != name:
                    continue
                for k in (q.get("exhausted") or []):
                    over.append(f"{k}（已用 {q['used'].get(k, '?')} / 上限 {q['hard'].get(k, '?')}）")
        except Exception:  # noqa: BLE001
            pass
        if over:
            out += ("\n⚠️ 注意：这些项现在**已经超限**，不会驱逐已有 Pod，"
                    "但从现在起**新建 Pod 会被拒绝**：" + "、".join(over))
    return out


def patch_pdb(k8s: K8sClient, ns: str, name: str,
              params: dict[str, Any], dry: str | None) -> str:
    """改 PodDisruptionBudget 的 minAvailable / maxUnavailable。

    ⚠️ 这两个字段是 ``IntOrString``：既可以是数字（`1`），也可以是百分比
    字符串（`"50%"`）。必须原样透传，**不能强制 int()**——早期版本把数组
    和字符串一律 str()，导致 `remove=["node-pool"]` 变成字符串 `"['node-pool']"`。
    这里保留类型：数字转 int，带 % 的保持字符串。

    另外 minAvailable 和 maxUnavailable **互斥**，同时给会被 API 拒绝。
    """
    min_a = params.get("min_available")
    max_u = params.get("max_unavailable")
    remove = params.get("remove") or []
    if min_a is None and max_u is None and not remove:
        raise ActionRejected("至少要给 min_available / max_unavailable / remove 之一")
    if min_a is not None and max_u is not None:
        raise ActionRejected(
            "min_available 和 max_unavailable 是互斥的，只能给一个——"
            "同时给会被 API 拒绝。想从一种换成另一种，用 remove 先删掉再设。")

    def _ios(v: Any) -> Any:
        """数字就转 int，带 % 的保持字符串。"""
        if isinstance(v, bool):
            raise ActionRejected("min_available/max_unavailable 不能是布尔值")
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        s = str(v).strip()
        if s.endswith("%"):
            return s
        try:
            return int(s)
        except ValueError as exc:
            raise ActionRejected(
                f"{v!r} 既不是数字也不是百分比（要写成 1 或 \"50%\"）") from exc

    body: list[dict[str, Any]] = []
    for key in remove:
        body.append({"op": "remove", "path": f"/spec/{key}"})
    if min_a is not None:
        body.append({"op": "add", "path": "/spec/minAvailable", "value": _ios(min_a)})
    if max_u is not None:
        body.append({"op": "add", "path": "/spec/maxUnavailable", "value": _ios(max_u)})

    try:
        k8s.policy_api.patch_namespaced_pod_disruption_budget(name, ns, body, dry_run=dry)
    except Exception as exc:  # noqa: BLE001
        raise ActionRejected(f"改 PDB 被拒绝: {getattr(exc, 'reason', exc)}") from exc
    return f"已更新 PDB/{name}：{json.dumps(params, ensure_ascii=False, default=str)[:200]}"
