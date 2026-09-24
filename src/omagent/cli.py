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

from .config import ROOT, ensure_local_bypass, load_env_file, llm_status
from .tools import FORBIDDEN, TOOLS

DEFAULT_USERS = ROOT / "var" / "users.json"


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    load_env_file()
    ensure_local_bypass()
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
        providers_factory=lambda k8s: _build_providers(k8s, args),
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


def cmd_eval(args: argparse.Namespace) -> int:
    """跑诊断质量评测。**会真调模型、真改集群。**"""
    from .agent import WRITE_NAMESPACES, OpsAgent
    from .audit import AuditLog
    from .cases import CaseStore
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

    # 案例库单独一份：评测不该污染真实积累的经验
    eval_cases = ROOT / "var" / "eval-cases.jsonl"

    def agent_factory() -> OpsAgent:
        return OpsAgent(
            K8sClient(kubeconfig=kubeconfig),
            AuditLog(ROOT / "var" / "eval-audit.jsonl"),
            operator="eval",
            write_namespaces=WRITE_NAMESPACES,
            cases=CaseStore(eval_cases),
        )

    print(f"评测 {len(scenarios)} 个剧本 × {args.repeat} 次，用真模型真集群。")
    print(f"模型：{llm.model} ｜ 每个剧本开始前恢复基线，结束后再恢复一次。")
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

    print(f"\n只读工具（{len(readonly)} 个）—— 自动执行，不需要确认\n")
    for t in readonly:
        print(f"  {t.name:<20} {t.summary}")
    print(f"\n写工具（{len(write)} 个）—— 必须人工批准后才会执行\n")
    for t in write:
        print(f"  {t.name:<20} {t.summary}")
    print(f"\n禁止动作（{len(FORBIDDEN)} 个）—— 不在工具清单里，模型看不见也调不到\n")
    for name, why in FORBIDDEN.items():
        print(f"  {name:<20} {why}")
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
    from .observability import LokiProvider, PrometheusProvider
    from .providers import ProviderRegistry

    if getattr(args, "no_observability", False):
        return ProviderRegistry()
    ns = getattr(args, "obs_namespace", "monitoring") or "monitoring"
    return ProviderRegistry([
        PrometheusProvider(k8s, namespace=ns,
                           url=getattr(args, "prometheus_url", "") or ""),
        LokiProvider(k8s, namespace=ns,
                     url=getattr(args, "loki_url", "") or ""),
    ])


def _add_provider_args(parser) -> None:
    parser.add_argument("--obs-namespace", default="monitoring",
                        help="Prometheus / Loki 所在的命名空间（默认 monitoring）")
    parser.add_argument("--prometheus-url", default="",
                        help="直连 Prometheus 的地址；不填则经 API Server service proxy")
    parser.add_argument("--loki-url", default="",
                        help="直连 Loki 的地址；不填则经 API Server service proxy")
    parser.add_argument("--no-observability", action="store_true",
                        help="不装配 Prometheus / Loki，只用内置 K8s 工具")


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
    ev.add_argument("--out", default=None, help="结果 JSON 写到哪")
    ev.add_argument("--rescore", default=None,
                    help="用当前预期重判一份已有报告（不重跑模型）")
    ev.add_argument("--min-pass", type=float, default=None,
                    help="通过率低于这个值就以非零码退出（给 CI 用）")
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
