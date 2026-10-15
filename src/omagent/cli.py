"""命令行入口。

    python -m omagent.cli serve            # 启动监控台（默认）
    python -m omagent.cli serve --demo     # 带故障注入按钮（演示用）
    python -m omagent.cli useradd <名字>    # 加一个能批准变更的运维
    python -m omagent.cli users            # 列出用户
    python -m omagent.cli tools            # 打印工具清单与读写切分
    python -m omagent.cli audit            # 校验审计链是否被篡改
    python -m omagent.cli status           # 打印模型配置状态
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, ensure_local_bypass, load_env_file, llm_status
from .tools import (
    FORBIDDEN,
    PERMANENTLY_FORBIDDEN,
    TOOLS,
    UNLOCKABLE,
    forbidden_now,
    unlock_tool,
    unlocked_tools,
)

DEFAULT_USERS = ROOT / "var" / "users.json"


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    load_env_file()
    ensure_local_bypass()

    # 解锁禁区工具。**在 serve 之前做完**，这样工具清单、审计回显、
    # 前端面板看到的都是同一份状态。
    # 写错了直接退出（unlock_tool 会抛）——不做静默忽略：参数敲错却照常起来，
    # 运维会以为自己已经打开了，实际没有。
    unlocked: list[str] = []
    for name in getattr(args, "allow_tool", None) or []:
        try:
            unlock_tool(name)
        except ValueError as exc:
            raise SystemExit(f"✗ --allow-tool {name}：{exc}")
        unlocked.append(name)
    if unlocked:
        # 必须在启动时**大声回显**：解锁禁区工具是这台机器上最值得被记住的
        # 一件配置，不能只存在于某个人的命令行历史里。
        print(f"⚠️  已解锁禁区工具：{'、'.join(unlocked)}")
        for n in unlocked:
            print(f"    - {n}: {UNLOCKABLE.get(n, '')}")
        print("    这些动作默认不可用，现在模型可以提议它们；"
              "仍需要人批准，且危险档位要求手输目标名字。")

    serve(
        host=args.host,
        port=args.port,
        operator=args.operator,
        namespace=args.namespace,
        kubeconfig=args.kubeconfig,
        demo=args.demo,
        write_namespaces=tuple(args.write_namespaces) if args.write_namespaces else None,
        model=args.model,
        users_path=args.users,
        session_dir=args.session_dir,
        allow_anonymous=args.allow_anonymous,
        bootstrap=not args.no_bootstrap,
        patrol_interval=args.patrol_interval,
        patrol_cooldown=args.patrol_cooldown,
        providers_factory=lambda k8s: _build_providers(k8s, args),
        unlocked=tuple(unlocked),
    )
    return 0


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------


def _ask_password(username: str, given: str | None) -> str:
    if given:
        return given
    p1 = getpass.getpass(f"为 {username} 设置密码（至少 8 位）：")
    p2 = getpass.getpass("再输一遍：")
    if p1 != p2:
        raise SystemExit("✗ 两次输入不一致")
    return p1


def cmd_useradd(args: argparse.Namespace) -> int:
    from .auth import ROLE_LABEL, ROLES, UserStore

    path = Path(args.users) if args.users else DEFAULT_USERS
    store = UserStore.load(path)
    if args.name in store.usernames and not args.force:
        raise SystemExit(f"✗ 用户 {args.name} 已存在（要改密码用 passwd，要重建加 --force）")
    password = _ask_password(args.name, args.password)
    store.add(args.name, password, args.role)
    store.save()
    print(f"✓ 已写入 {path}")
    print(f"  {args.name} — {ROLE_LABEL.get(args.role, args.role)}")
    if args.role == "viewer":
        print("  提示：viewer 能看、能问，但**不能批准变更**。")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    from .auth import UserStore

    path = Path(args.users) if args.users else DEFAULT_USERS
    store = UserStore.load(path)
    if args.name not in store.usernames:
        raise SystemExit(f"✗ 用户 {args.name} 不存在")
    store.set_password(args.name, _ask_password(args.name, args.password))
    store.save()
    print(f"✓ 已更新 {args.name} 的密码")
    return 0


def cmd_userdel(args: argparse.Namespace) -> int:
    from .auth import UserStore

    path = Path(args.users) if args.users else DEFAULT_USERS
    store = UserStore.load(path)
    if not store.remove(args.name):
        raise SystemExit(f"✗ 用户 {args.name} 不存在")
    store.save()
    print(f"✓ 已删除 {args.name}")
    return 0


def make_eval_agent_factory(
    args: argparse.Namespace,
    kubeconfig: str | None = None,
    *,
    k8s_factory: Callable[[], Any] | None = None,
    audit_path: str | Path | None = None,
    cases_path: str | Path | None = None,
) -> Callable[[], Any]:
    """造评测用的 agent 工厂。

    ⚠️ **和 serve 共用同一套 provider 装配逻辑**（``_build_providers``）。

    以前这里**没传 ``providers=``**，于是全部剧本是在"Prometheus / Loki /
    变更历史全关掉"的配置下跑的——**评测测的根本不是真实配置**。
    而"依赖变慢"这一类故障恰恰只有指标/日志能定位，所以那个数字对不上线。

    复制一份装配逻辑是不行的：两份迟早漂移，而"评测跑的配置"和"线上跑的
    配置"必须是同一个。所以这里直接复用 ``_build_providers``，
    参数也复用 ``_add_provider_args``。
    """
    from .agent import WRITE_NAMESPACES, OpsAgent
    from .audit import AuditLog
    from .cases import CaseStore
    from .k8s import K8sClient

    make_k8s = k8s_factory or (lambda: K8sClient(kubeconfig=kubeconfig))
    audit = Path(audit_path) if audit_path else (ROOT / "var" / "eval-audit.jsonl")
    cases = Path(cases_path) if cases_path else (ROOT / "var" / "eval-cases.jsonl")

    def agent_factory() -> OpsAgent:
        k8s = make_k8s()
        return OpsAgent(
            k8s,
            AuditLog(audit),
            operator="eval",
            write_namespaces=WRITE_NAMESPACES,
            cases=CaseStore(cases, runbooks=ROOT / "runbooks.jsonl"),
            # 默认接上 Prometheus / Loki / 变更历史（= 真实配置）；
            # 想对比"有无数据源的差异"用 --no-observability 显式关。
            providers=_build_providers(k8s, args),
        )

    return agent_factory


def _print_providers(registry) -> None:
    """把"这次评测到底接上了哪些数据源"打出来。

    不打印的话，"这批数字是在什么配置下跑的"又变成一个只能靠命令行历史
    回答的问题——而"评测跑的是真实配置"恰恰是这次要钉住的事。
    """
    for p in registry.status():
        if p["available"]:
            print(f"  数据源：{p['name']}（{p['tools']} 个只读工具）")
        else:
            print(f"  数据源：{p['name']} 不可用 — {p['reason'][:70]}")


def cmd_eval(args: argparse.Namespace) -> int:
    """跑诊断质量评测。**会真调模型、真改集群。**"""
    from .evals import SCENARIOS, dump, render, run, select
    from .k8s import K8sClient
    from .llm import LLMClient

    load_env_file()
    ensure_local_bypass()

    if args.rescore:
        from .evals import load_report, rescore

        rep = rescore(load_report(args.rescore))
        print(render(rep))
        out = args.out or args.rescore
        dump(rep, out)
        print(f"\n重判结果：{out}")
        return 0

    scenarios = select(args.only)
    if not scenarios:
        raise SystemExit(f"✗ 没有匹配的剧本。可选：{', '.join(s.name for s in SCENARIOS)}")

    kubeconfig = args.kubeconfig or str(ROOT / "var" / "kubeconfig")
    llm = LLMClient(model=args.model)
    if not llm.available:
        raise SystemExit("✗ 没配置模型 API Key，评测跑不了（评测必须用真模型）")

    agent_factory = make_eval_agent_factory(args, kubeconfig)

    print(f"评测 {len(scenarios)} 个剧本 × {args.repeat} 次，用真模型真集群。")
    print(f"模型：{llm.model} ｜ 每个剧本开始前恢复基线，结束后再恢复一次。")
    # 先探一次数据源，把配置说在数字前面。
    try:
        _print_providers(_build_providers(K8sClient(kubeconfig=kubeconfig), args))
    except Exception as exc:  # noqa: BLE001
        # 探不到不该拦住评测：provider 连不上是功能降级（注册表会跳过）。
        print(f"  数据源：探测失败 — {type(exc).__name__}: {str(exc)[:70]}")
    print("⚠️ 期间请不要同时操作演示集群。\n")

    report = run(scenarios,
                 agent_factory=agent_factory,
                 llm_factory=lambda: LLMClient(model=args.model),
                 repeat=args.repeat,
                 settle=args.settle,
                 on_log=lambda m: print(m, flush=True))

    print(render(report))
    out = args.out or str(ROOT / "var" / "eval-report.json")
    dump(report, out)
    print(f"\n完整结果：{out}")

    threshold = args.min_pass
    if threshold is not None and report["pass_rate"] < threshold:
        print(f"✗ 通过率 {report['pass_rate'] * 100:.0f}% 低于阈值 {threshold * 100:.0f}%")
        return 1
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    from .cases import CaseStore, describe

    path = Path(args.path) if args.path else (ROOT / "var" / "cases.jsonl")
    store = CaseStore(path)
    cases = store.all()
    if not cases:
        print(f"案例库：{path}")
        print("  （空）—— 案例在「人工批准 + 执行成功」之后自动入库。")
        return 0
    from .cases import OUTCOME_EFFECTIVE, OUTCOME_INEFFECTIVE

    stats = store.stats()
    by = stats["by_outcome"]
    label = {OUTCOME_EFFECTIVE: "已证实有效", OUTCOME_INEFFECTIVE: "回查无效",
             "applied": "待回查"}
    print(f"案例库：{path}")
    print(f"  共 {stats['total']} 条，涉及 {stats['workloads']} 个工作负载")
    print("  " + " ｜ ".join(f"{label.get(k, k)} {v}" for k, v in sorted(by.items())))
    print(f"  **可被检索的只有『已证实有效』那 {stats['teachable']} 条**——"
          f"其它要么还没回查，要么被证明没用")
    if stats["tools"]:
        print(f"  出现过的处置：{'、'.join(stats['tools'])}")
    print()
    for c in cases[-args.limit:][::-1]:
        mark = {OUTCOME_EFFECTIVE: "✓", OUTCOME_INEFFECTIVE: "✗"}.get(c.outcome, "…")
        print(f"  {mark} [{c.ts[:19].replace('T', ' ')}] {c.workload} · {c.tool}"
              f"  ({label.get(c.outcome, c.outcome)})")
        print(f"      症状：{describe(c.signature)}")
        print(f"      理由：{(c.rationale or '')[:90]}")
        if c.evidence:
            print(f"      回查：{c.evidence[:90]}")
        print(f"      批准人：{c.operator}")
    return 0


def cmd_users(args: argparse.Namespace) -> int:
    from .auth import ROLE_LABEL, UserStore

    path = Path(args.users) if args.users else DEFAULT_USERS
    store = UserStore.load(path)
    print(f"用户库：{path}")
    if len(store) == 0:
        print("  （空）—— 注意：serve 在没有用户时会**拒绝启动**，")
        print("   除非显式加 --allow-anonymous（仅限本机一次性演示）。")
        return 0
    for u in store.to_dict()["users"]:
        print(f"  {u['username']:<20} {ROLE_LABEL.get(u['role'], u['role'])}")
    return 0


def cmd_tools(_: argparse.Namespace) -> int:
    readonly = [t for t in TOOLS.values() if not t.mutating]
    write = [t for t in TOOLS.values() if t.mutating]
    opened = unlocked_tools()

    print(f"\n只读工具（{len(readonly)} 个）—— 自动执行，不需要确认\n")
    for t in readonly:
        print(f"  {t.name:<20} {t.summary}")
    print(f"\n写工具（{len(write)} 个）—— 必须人工批准后才会执行\n")
    for t in write:
        print(f"  {t.name:<20} {t.summary}")
    now = forbidden_now()
    print(f"\n禁止动作（{len(now)} 个）—— 不在工具清单里，模型看不见也调不到\n")
    for name, why in now.items():
        tag = "（永久禁止）" if name in PERMANENTLY_FORBIDDEN else ""
        print(f"  {name:<20} {why}{tag}")

    if opened:
        print(f"\n⚠️  已解锁（{len(opened)} 个）—— 本进程用 --allow-tool 显式打开了\n")
        for spec in opened.values():
            print(f"  {spec.name:<20} danger={spec.danger}  {spec.summary}")
        print("\n  这些动作**仍然需要人批准**，且 severe 档位要求手输目标名字。")
    else:
        print(f"\n可解锁但当前锁着：{'、'.join(sorted(UNLOCKABLE))}"
              f"（用 --allow-tool <名字> 打开，须在 serve 启动时指定）")
    print()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit import AuditLog

    path = args.path or (ROOT / "var" / "audit.jsonl")
    audit = AuditLog(path)
    ok, msg = audit.verify()
    print(f"审计日志：{path}")
    print(f"链校验：{'✅ 通过' if ok else '❌ 失败'} — {msg}")
    return 0 if ok else 1


def cmd_status(_: argparse.Namespace) -> int:
    load_env_file()
    st = llm_status()
    print(f"模型接口：{st['base_url']}")
    print(f"模型名称：{st['model']}")
    print(f"API Key ：{st['api_key']}")
    print(f"可用    ：{st['available']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """上线前自检：把"跑不起来"的原因在启动之前说清楚。

    存在的理由很直接——这个项目最难的一步不是装依赖，是**权限**。
    同类 CLI 工具（k8sgpt、kubectl-ai）挂在你已有的 kubeconfig 上就完事，
    而这个项目要读、还要**写**，所以它需要一份比只读大、比 cluster-admin 小的
    RBAC。配错了的表现是"页面能打开，但一问就报错"，很难自己查出来。

    所以这里逐项检查，并且**每一项失败都给一条能直接粘的命令**。
    """
    from .config import ROOT as _ROOT
    from .k8s import K8sClient, K8sUnavailable

    load_env_file()
    results: list[tuple[str, bool, str, str]] = []   # 名称, 通过, 说明, 修复

    def check(name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
        results.append((name, ok, detail, fix))
        return ok

    # --- 1. 依赖 -----------------------------------------------------------
    missing = []
    for mod in ("kubernetes", "yaml", "requests"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    check("Python 依赖", not missing,
          "、".join(missing) + " 导入失败" if missing else "kubernetes / pyyaml / requests 就绪",
          "bash scripts/setup-python.sh")

    # --- 2. kubeconfig -----------------------------------------------------
    import os as _os
    kc = args.kubeconfig or _os.environ.get("KUBECONFIG")
    if kc:
        exists = _os.path.exists(kc)
        check("KUBECONFIG", exists,
              f"指向 {kc}" + ("" if exists else "（文件不存在）"),
              "export KUBECONFIG=/path/to/kubeconfig")
    else:
        default_kc = _os.path.expanduser("~/.kube/config")
        check("KUBECONFIG", _os.path.exists(default_kc),
              "环境变量没设，回退到 ~/.kube/config"
              + ("（存在）" if _os.path.exists(default_kc) else "（不存在）"),
              "export KUBECONFIG=/path/to/kubeconfig")

    # --- 3. 集群可达 -------------------------------------------------------
    k8s = None
    try:
        k8s = K8sClient(kubeconfig=kc)
        ok, info = k8s.ping()
    except K8sUnavailable as exc:
        ok, info = False, str(exc)
    check("集群可达", ok, info,
          "检查 kubeconfig 是否指对了集群；集群没起的话先起（沙箱见 scripts/setup-sandbox.sh）")

    if not k8s or not ok:
        _doctor_report(results)
        return 1

    # --- 4/5. RBAC：读得到吗、写得动吗 -------------------------------------
    # 用 SelfSubjectAccessReview 直接问 API Server，比自己试着调一次干净：
    # 没有副作用，而且 403 和 404 不会混。
    ns_list = list(args.namespace or ["demo"])
    write_ns = list(args.write_namespaces or ("demo", "staging"))
    read_ok, read_detail = _can_i(k8s, "list", "", "pods", ns_list[0])
    check("只读权限", read_ok,
          f"namespace={ns_list[0]} 列 pods：{'允许' if read_ok else '被拒绝'}",
          f"需要一个能 get/list/watch pods 的 Role（namespace {ns_list[0]}）")

    for wns in write_ns:
        w_ok, w_detail = _can_i(k8s, "patch", "apps", "deployments", wns)
        check(f"写权限 · {wns}", w_ok,
              f"patch deployments：{'允许' if w_ok else '被拒绝（审批通过后仍然执行不了）'}",
              f"授予 namespace {wns} 内 deployments 的 patch/update 权限；"
              f"只读部署可以忽略这一项，但要接受「改动执行不了」")

    # --- 命名空间到底存不存在 ---------------------------------------------
    #
    # 上面两个检查用的是 SelfSubjectAccessReview，它只回答"你有没有权限"，
    # **不回答"这东西存不存在"**——所以给一个拼错的命名空间，自检照样全绿。
    # 这正是最典型的首次失败：装好了、跑起来了、页面打开一片空白，
    # 而人完全不知道为什么。同类 CLI 工具挂上 kubeconfig 就能用，
    # 恰恰是因为它们不需要你报一个命名空间。
    try:
        existing = set(k8s.namespaces())
    except Exception as exc:  # noqa: BLE001
        existing = set()
        check("命名空间", False, f"列不出来：{exc}", "确认账号有 list namespaces 权限")
    if existing:
        hint = "、".join(sorted(existing)[:8]) + ("…" if len(existing) > 8 else "")
        for check_ns in dict.fromkeys([*ns_list, *write_ns]):
            if check_ns in existing:
                continue
            check(f"命名空间 · {check_ns}", False,
                  "不存在——页面会是空的，而且不报错",
                  f"换一个真实存在的：--namespace <名字>；当前集群里有 {hint}")
        if "demo" not in existing and "demo" in write_ns:
            # serve 的默认命名空间是 demo（沙箱约定）。真实集群里通常没有它，
            # 不说清楚的话，人只会看到一个空控制台然后以为坏了。
            check("默认命名空间", False,
                  "集群里没有 demo（serve 的默认值）",
                  "启动时显式指定：omagent serve --namespace <你的命名空间>")

    # --- 6. metrics-server --------------------------------------------------
    # 没有它，get_metrics 会返回空——而"内存够不够"恰恰是最常问的问题。
    try:
        metrics_ok = k8s.metrics_available()
    except Exception:  # noqa: BLE001
        metrics_ok = False
    check("metrics-server", metrics_ok,
          "metrics.k8s.io 可用" if metrics_ok else "没装，get_metrics 会返回空",
          "kubectl apply -f sandbox/addons/metrics-server.yaml")

    # --- 7. 模型 -----------------------------------------------------------
    st = llm_status()
    check("模型 API Key", bool(st["available"]),
          f"{st['model']} @ {st['base_url']}（{'已配置' if st['available'] else '未配置'}）",
          "在仓库根目录写 .env：DEEPSEEK_API_KEY=sk-...")

    # --- 8. 用户库 ---------------------------------------------------------
    users_path = args.users or (_ROOT / "var" / "users.json")
    from pathlib import Path as _Path
    has_users = _Path(users_path).exists()
    check("用户库", True,
          f"{users_path} " + ("已存在" if has_users else "不存在——首次启动会自动创建 admin 账号并打印随机密码"),
          f"python -m omagent.cli useradd <用户名> --role operator")

    return _doctor_report(results)


def _can_i(k8s, verb: str, group: str, resource: str, namespace: str) -> tuple[bool, str]:
    """用 SelfSubjectAccessReview 问一句"我能不能干这个"。"""
    try:
        from kubernetes import client as _c
        review = _c.V1SelfSubjectAccessReview(
            spec=_c.V1SelfSubjectAccessReviewSpec(
                resource_attributes=_c.V1ResourceAttributes(
                    verb=verb, group=group or None, resource=resource, namespace=namespace
                )
            )
        )
        resp = k8s.authz.create_self_subject_access_review(review)
        return bool(resp.status.allowed), resp.status.reason or ""
    except Exception as exc:  # noqa: BLE001
        return False, f"检查失败：{exc}"


def _doctor_report(results: list[tuple[str, bool, str, str]]) -> int:
    failed = [r for r in results if not r[1]]
    print("\n环境自检\n" + "─" * 66)
    for name, ok, detail, _fix in results:
        print(f"  {'✓' if ok else '✗'} {name:<18} {detail}")
    if not failed:
        print("─" * 66)
        print("全部通过。启动：python -m omagent.cli serve\n")
        return 0
    print("─" * 66)
    print(f"{len(failed)} 项需要处理：\n")
    for name, _ok, _detail, fix in failed:
        if fix:
            print(f"  {name}")
            print(f"    → {fix}")
    print()
    return 1



def _build_providers(k8s, args):
    """按命令行参数装配外部只读数据源。

    默认去 ``monitoring`` 命名空间找 Prometheus / Loki，并且**经 API Server 的
    service proxy** 访问——复用现有 kubeconfig，不需要 port-forward 或 ingress。
    真实部署里想直连 Service DNS，用 --prometheus-url / --loki-url 覆盖。

    连不上不算错误：注册表会跳过它，内置 K8s 工具照常工作。
    外部依赖缺失应该是功能降级，不是启动失败。
    """
    from .changehistory import ChangeHistoryProvider
    from .observability import LokiProvider, PrometheusProvider
    from .providers import ProviderRegistry

    providers: list = []
    # 变更历史纯读 K8s，不需要任何外部系统，所以**默认总是开**。
    # 它直接对着评测里最稳定的三个失败（probe/crash/cordon）：
    # 那三个的共同根因是"模型不会把症状和最近一次变更联系起来"。
    providers.append(ChangeHistoryProvider(k8s, namespace=getattr(args, "namespace", "demo")))
    if not getattr(args, "no_observability", False):
        ns = getattr(args, "obs_namespace", "monitoring") or "monitoring"
        providers += [
            PrometheusProvider(k8s, namespace=ns,
                               url=getattr(args, "prometheus_url", "") or ""),
            LokiProvider(k8s, namespace=ns,
                         url=getattr(args, "loki_url", "") or ""),
        ]
    providers += _build_mcp_providers(args)
    return ProviderRegistry(providers)


def _build_mcp_providers(args) -> list:
    """把 --mcp-server / --mcp-allow-read 解析成 McpClientProvider。

    格式：
        --mcp-server "github=npx -y @modelcontextprotocol/server-github"
        --mcp-allow-read github:list_issues --mcp-allow-read github:get_file

    **不写 --mcp-allow-read 的话，那个 server 的工具一个都不会导入**
    （默认拒绝）。MCP 协议里远程工具能自报 readOnlyHint，但那只是对端的一句话，
    不能当作凭据——所以必须由人来点名。
    """
    import shlex
    from .mcp_client import McpClientProvider

    servers = getattr(args, "mcp_server", None) or []
    allows = getattr(args, "mcp_allow_read", None) or []

    allow_map: dict[str, list[str]] = {}
    for spec in allows:
        if ":" not in spec:
            raise SystemExit(f"✗ --mcp-allow-read 要写成 服务名:工具名，收到 {spec!r}")
        srv, tool = spec.split(":", 1)
        allow_map.setdefault(srv.strip(), []).append(tool.strip())

    out = []
    for spec in servers:
        if "=" not in spec:
            raise SystemExit(f"✗ --mcp-server 要写成 名字=命令，收到 {spec!r}")
        name, cmd = spec.split("=", 1)
        name = name.strip()
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            raise SystemExit(f"✗ --mcp-server {name} 的命令解析失败：{exc}") from exc
        if not argv:
            raise SystemExit(f"✗ --mcp-server {name} 没给命令")
        out.append(McpClientProvider(name, argv,
                                     allow_readonly=tuple(allow_map.get(name, ()))))
    return out


def _add_provider_args(parser) -> None:
    parser.add_argument("--obs-namespace", default="monitoring",
                        help="Prometheus / Loki 所在的命名空间（默认 monitoring）")
    parser.add_argument("--prometheus-url", default="",
                        help="直连 Prometheus 的地址；不填则经 API Server service proxy")
    parser.add_argument("--loki-url", default="",
                        help="直连 Loki 的地址；不填则经 API Server service proxy")
    parser.add_argument("--no-observability", action="store_true",
                        help="不装配 Prometheus / Loki，只用内置 K8s 工具")
    parser.add_argument("--mcp-server", action="append", metavar="名字=命令",
                        help='接一个外部 MCP server，可重复。例：--mcp-server "fs=npx -y @modelcontextprotocol/server-filesystem /data"')
    parser.add_argument("--mcp-allow-read", action="append", metavar="服务名:工具名",
                        help="把某个外部工具加入只读白名单（**不写就不导入**），可重复")


def _add_unlock_args(parser) -> None:
    """`--allow-tool` 只挂在 `serve` 上，**不挂给 `mcp`**。

    它原来被 `_add_provider_args()` 顺手挂到了 serve 和 mcp 两个子命令上，
    而 `cmd_mcp()` 根本不调用 `unlock_tool()`——于是 `omagent mcp --allow-tool delete_pvc`
    会被**静默接受、毫无效果**：参数写错了却照常起来，人还以为打开了。
    这正好违反本项目自己的规矩（启动参数写错就直接退出，不静默忽略）。

    而且 MCP server 只暴露只读工具，写操作本来就不会上去，
    在这儿解锁没有任何意义。所以在源头上去掉，而不是在 cmd_mcp 里补一句报错。
    """
    parser.add_argument("--allow-tool", action="append", metavar="工具名", default=[],
                        help="解锁一个默认锁着的禁区工具（可重复）。当前可解锁："
                             + "、".join(sorted(UNLOCKABLE))
                             + "。**不写就永远锁着**；每个都要单独指定，"
                               "启动时会回显、并写进审计。")


def cmd_mcp(args: argparse.Namespace) -> int:
    """以 MCP server 方式跑（stdio），把只读工具暴露给任意 MCP 客户端。

    stdout 是协议通道，所以**任何提示都必须走 stderr**，否则会污染 JSON-RPC 流。
    """
    from .agent import WRITE_NAMESPACES, OpsAgent
    from .audit import AuditLog
    from .cases import CaseStore
    from .k8s import K8sClient, K8sUnavailable
    from .mcp import McpServer
    from .tools import READONLY_TOOLS

    load_env_file()
    try:
        k8s = K8sClient(kubeconfig=args.kubeconfig)
    except K8sUnavailable as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    agent = OpsAgent(
        k8s,
        AuditLog(args.audit_path or (ROOT / "var" / "audit.jsonl")),
        operator=f"mcp:{args.client or 'unknown'}",
        write_namespaces=WRITE_NAMESPACES,
        cases=CaseStore(ROOT / "var" / "cases.jsonl"),
    )
    ok, detail = agent.cluster_status()
    if not ok:
        print(f"✗ 连不上集群：{detail}", file=sys.stderr)
        return 1

    print(f"omagent MCP server 就绪 ｜ 集群 {detail} ｜ 命名空间 {args.namespace} ｜ "
          f"{len(READONLY_TOOLS)} 个只读工具（写操作不在此暴露）", file=sys.stderr)
    return McpServer(agent, namespace=args.namespace).serve()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omagent", description="O&M Agent —— 对话式 K8s 运维助手")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="启动监控台")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--operator", default="operator",
                   help="仅在 --allow-anonymous 时用作操作员标识")
    s.add_argument("--namespace", default="demo", help="默认命名空间")
    s.add_argument("--kubeconfig", default=None)
    s.add_argument("--model", default=None, help="覆盖模型名（默认读 OMAGENT_LLM_MODEL）")
    s.add_argument("--demo", action="store_true", help="开放沙箱故障注入按钮")
    s.add_argument("--write-namespaces", nargs="*", default=None,
                   help="允许写操作的命名空间（默认 demo、staging）")
    s.add_argument("--users", default=None, help=f"用户库路径（默认 {DEFAULT_USERS}）")
    s.add_argument("--session-dir", default=None, help="对话持久化目录（默认 var/sessions）")
    s.add_argument("--allow-anonymous", action="store_true",
                   help="关闭鉴权（⚠️ 任何人都能批准写操作，只在本机一次性演示时用）")
    s.add_argument("--no-bootstrap", action="store_true",
                   help="用户库不存在时不自动建 admin 账号，直接报错退出")
    _add_provider_args(s)
    _add_unlock_args(s)
    s.add_argument("--patrol-interval", type=float, default=0.0,
                   help="巡检间隔秒数（0=不启用）。启用后会主动扫集群、自动起会话调查")
    s.add_argument("--patrol-cooldown", type=float, default=1800.0,
                   help="同一目标多久内不重复报（默认 1800 秒）")
    s.set_defaults(func=cmd_serve)

    t = sub.add_parser("tools", help="打印工具清单与读写切分")
    t.set_defaults(func=cmd_tools)

    a = sub.add_parser("audit", help="校验审计链")
    a.add_argument("--path", default=None)
    a.set_defaults(func=cmd_audit)

    st = sub.add_parser("status", help="打印模型配置状态")
    st.set_defaults(func=cmd_status)

    # --- 用户管理 ---
    ua = sub.add_parser("useradd", help="新增用户")
    ua.add_argument("name")
    ua.add_argument("--role", choices=["operator", "viewer"], default="operator",
                    help="operator 能批准变更；viewer 只能看和问（默认 operator）")
    ua.add_argument("--password", default=None,
                    help="直接给密码（不推荐：会留在 shell 历史里；不传则交互式输入）")
    ua.add_argument("--users", default=None)
    ua.add_argument("--force", action="store_true", help="同名用户已存在时覆盖")
    ua.set_defaults(func=cmd_useradd)

    pw = sub.add_parser("passwd", help="改密码")
    pw.add_argument("name")
    pw.add_argument("--password", default=None)
    pw.add_argument("--users", default=None)
    pw.set_defaults(func=cmd_passwd)

    ud = sub.add_parser("userdel", help="删除用户")
    ud.add_argument("name")
    ud.add_argument("--users", default=None)
    ud.set_defaults(func=cmd_userdel)

    ev = sub.add_parser("eval", help="诊断质量评测（真模型真集群，会改集群状态）")
    ev.add_argument("--only", default="", help="只跑这些剧本，逗号分隔，如 oom,probe")
    ev.add_argument("--repeat", type=int, default=1, help="每个剧本跑几次（看稳定性）")
    ev.add_argument("--settle", type=float, default=20.0, help="注入故障后等多少秒让它显现")
    ev.add_argument("--model", default=None)
    ev.add_argument("--kubeconfig", default=None)
    ev.add_argument("--namespace", default="demo",
                    help="provider（Prometheus / Loki 之外的变更历史）默认命名空间；"
                         "剧本各自的命名空间不受影响")
    ev.add_argument("--out", default=None, help="结果 JSON 写到哪")
    ev.add_argument("--rescore", default=None,
                    help="用当前预期重判一份已有报告（不重跑模型）")
    ev.add_argument("--min-pass", type=float, default=None,
                    help="通过率低于这个值就以非零码退出（给 CI 用）")
    # ⚠️ provider 参数**和 serve 是同一套定义**（同一个 _add_provider_args）：
    #    --obs-namespace / --prometheus-url / --loki-url / --no-observability。
    #    默认是**接上**（评测要测真实配置），--no-observability 只是留一个
    #    "显式关掉"的开关，用来对比有无数据源的差异。
    #    在这里另写一份参数迟早和 serve 漂移，所以直接复用。
    _add_provider_args(ev)
    ev.set_defaults(func=cmd_eval)

    ca = sub.add_parser("cases", help="查看案例库（语义记忆）")
    ca.add_argument("--path", default=None)
    ca.add_argument("--limit", type=int, default=20)
    ca.set_defaults(func=cmd_cases)

    us = sub.add_parser("users", help="列出用户")
    us.add_argument("--users", default=None)
    us.set_defaults(func=cmd_users)

    d = sub.add_parser("doctor", help="上线前自检：集群连通性、RBAC、metrics-server、模型配置")
    d.add_argument("--kubeconfig", default=None)
    d.add_argument("--namespace", nargs="*", default=None, help="用于只读权限检查的命名空间")
    d.add_argument("--write-namespaces", nargs="*", default=None,
                   help="检查写权限的命名空间（默认 demo、staging）")
    d.add_argument("--users", default=None)
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("mcp", help="以 MCP server 运行（stdio，只读工具）")
    m.add_argument("--namespace", default="demo", help="默认命名空间")
    m.add_argument("--kubeconfig", default=None)
    m.add_argument("--client", default="", help="客户端标识，写进审计的操作员字段")
    m.add_argument("--audit-path", default=None)
    _add_provider_args(m)
    m.set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # 不带子命令时默认 serve
    if not argv or argv[0].startswith("-"):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
