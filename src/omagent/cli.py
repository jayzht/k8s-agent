"""命令行入口与演示编排。

这里最重要的一件东西是**确认卡片**（PRD 5.4）。在 L1 档位下，产品力几乎
全部体现在这张卡片上：它要在 30 秒内让人敢按下确认键。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .agent import TOOLS, OpsAgent, Refusal
from .audit import AuditLog, new_trace_id
from .impact import summarise
from .k8s import K8sClient, K8sUnavailable
from .models import Decision, Proposal
from .planner import LLMPlanner, RuleBasedPlanner

console = Console()
ROOT = Path(__file__).resolve().parents[2]

TIER_STYLE = {"T0": "dim", "T1": "bold green", "T2": "bold yellow", "T3": "bold red"}


# ---------------------------------------------------------------------------
# 确认卡片
# ---------------------------------------------------------------------------


def render_card(prop: Proposal) -> Panel:
    """渲染确认卡片。字段顺序刻意固定：先结论、再证据、再影响面、最后回滚。"""
    tier = prop.effective_tier
    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold")
    head.add_column()
    head.add_row("动作", f"[cyan]{prop.tool}[/cyan]  {prop.params}")
    head.add_row("目标", str(prop.target))
    tier_txt = f"[{TIER_STYLE[tier.value]}]{tier.value} {tier.label}[/]"
    if prop.escalated_to:
        tier_txt += (
            f"  [yellow]（由 {prop.tier.value} 升级为 {prop.escalated_to.value}）[/yellow]"
        )
    head.add_row("风险等级", tier_txt)
    head.add_row("确认强度", tier.confirm_strength)

    # --- 为什么要做 ---
    why = Group(
        Text(prop.rationale or "（未提供理由）", style="white"),
        Text(""),
        *[Text(f"  证据： {e.render()}", style="dim") for e in prop.evidence[:6]],
    )

    # --- 影响面 ---
    imp = Table.grid(padding=(0, 2))
    imp.add_column(style="bold")
    imp.add_column()
    for label, value in summarise(prop.impact):
        imp.add_row(label, value)
    for note in prop.impact.notes:
        imp.add_row("", f"[yellow]• {note}[/yellow]")

    # --- dry-run ---
    if prop.tier.forbidden:
        # T3 在 propose() 里就短路了，从未进入 dry-run，不能显示成"未通过"
        dry = Text("— 禁止动作，未进入 dry-run（直接拒绝）", style="red")
    elif prop.tier.requires_approval:
        if prop.dry_run_ok:
            dry = Text(f"✅ 服务端 dry-run 通过 — {prop.dry_run_output}", style="green")
        else:
            dry = Text(f"❌ 服务端 dry-run 未通过 — {prop.dry_run_output}", style="red")
    else:
        dry = Text("— 只读操作，无需 dry-run", style="dim")

    # --- 回滚 ---
    rb = Group(
        Text(prop.rollback or "无自动回滚路径", style="white"),
        Text(f"预计恢复时间：{prop.rollback_eta}", style="dim"),
    )

    # --- 熔断 ---
    if prop.breaches:
        cb = Group(
            *[
                Text(
                    b.render(),
                    style={"block": "red", "escalate": "yellow", "warn": "dim"}[b.severity],
                )
                for b in prop.breaches
            ]
        )
    else:
        cb = Text("✅ 全部熔断检查通过", style="green")

    body = Group(
        Rule("[bold]建议动作[/bold]", align="left"),
        head,
        Rule("[bold]为什么要做[/bold]", align="left"),
        why,
        Rule("[bold]影响面[/bold]", align="left"),
        imp,
        Rule("[bold]dry-run 校验[/bold]", align="left"),
        dry,
        Rule("[bold]回滚[/bold]", align="left"),
        rb,
        Rule("[bold]熔断检查[/bold]", align="left"),
        cb,
    )

    status = "blocked" if prop.blocked else ("needs-approval" if prop.requires_approval else "auto")
    border = {"blocked": "red", "needs-approval": "yellow", "auto": "green"}[status]
    title = {
        "blocked": "⛔ 方案已被熔断拦截",
        "needs-approval": "⚠️ 待人工确认",
        "auto": "✅ 只读操作已自动执行",
    }[status]
    return Panel(body, title=title, border_style=border, padding=(1, 2))


# ---------------------------------------------------------------------------
# 确认器
# ---------------------------------------------------------------------------


class CLIConfirmer:
    """交互式确认：渲染卡片，等人按 y。"""

    def __init__(self, operator: str):
        self.operator = operator

    def confirm(self, prop: Proposal) -> Decision:
        console.print()
        console.print(render_card(prop))
        console.print()
        if prop.blocked:
            console.print("[red]该方案已被熔断规则拦截，无法执行。[/red]")
            return Decision(prop.proposal_id, False, self.operator, "熔断拦截")
        if not prop.requires_approval:
            return Decision(prop.proposal_id, True, self.operator, "只读操作自动放行")

        strength = prop.effective_tier.confirm_strength
        if strength == "strong":
            console.print("[yellow]这是中危操作，需要填写变更理由。[/yellow]")
        ok = Confirm.ask(
            f"[bold]确认执行 {prop.tool} → {prop.target} ?[/bold]", default=False
        )
        reason = ""
        if ok and strength == "strong":
            from rich.prompt import Prompt

            reason = Prompt.ask("变更理由（会写入审计日志）")
        return Decision(prop.proposal_id, ok, self.operator, reason or ("人工确认" if ok else "人工取消"))


class AutoConfirmer:
    """演示用：预设裁决，模拟某个操作人。"""

    def __init__(self, operator: str, approve: bool = True, reason: str = "演示脚本自动确认"):
        self.operator = operator
        self.approve = approve
        self.reason = reason

    def confirm(self, prop: Proposal) -> Decision:
        console.print()
        console.print(render_card(prop))
        console.print()
        return Decision(prop.proposal_id, self.approve, self.operator, self.reason)


# ---------------------------------------------------------------------------
# 诊断结果渲染
# ---------------------------------------------------------------------------


def render_diagnosis(diag) -> None:
    conf_style = {"high": "green", "medium": "yellow", "low": "red"}.get(diag.confidence, "white")
    console.print(
        Panel(
            Text(diag.conclusion, style="white"),
            title=f"🔍 诊断结论  [dim]signature={diag.matched_signature}[/dim]",
            subtitle=f"置信度 [{conf_style}]{diag.confidence}[/]",
            border_style="blue",
        )
    )
    if diag.evidence:
        t = Table(title="证据链", show_header=True, header_style="bold")
        t.add_column("#", style="dim", width=3)
        t.add_column("类型", width=8)
        t.add_column("对象")
        t.add_column("内容")
        for i, e in enumerate(diag.evidence, 1):
            t.add_row(str(i), e.kind, e.ref, e.detail[:160])
        console.print(t)
    for f in diag.findings:
        console.print(f"  [dim]• {f}[/dim]")
    if diag.candidates:
        t = Table(title="候选处置动作", show_header=True, header_style="bold")
        t.add_column("#", style="dim", width=3)
        t.add_column("动作")
        t.add_column("理由")
        t.add_column("备注", style="yellow")
        for i, c in enumerate(diag.candidates, 1):
            t.add_row(str(i), c.tool, c.rationale[:90], c.note[:80])
        console.print(t)


def render_refusal(r: Refusal) -> None:
    console.print(
        Panel(
            Text(r.render(), style="red"),
            title="⛔ 请求被拒绝",
            border_style="red",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def build_agent(args) -> tuple[OpsAgent, AuditLog]:
    k8s = K8sClient(kubeconfig=args.kubeconfig)
    from .policy import Policy

    policy = Policy(args.policy) if args.policy else Policy()
    audit = AuditLog(args.audit)
    agent = OpsAgent(
        k8s, policy, audit, operator=args.operator, dry_run_only=args.read_only
    )
    return agent, audit


def cmd_status(args) -> int:
    # 先展示 LLM 配置：集群连不上时，这恰恰是最需要看到的信息
    from .config import llm_status

    st = llm_status()
    flag = (
        "[green]可用[/green]"
        if st["available"] == "是"
        else "[yellow]不可用（诊断将降级到规则引擎）[/yellow]"
    )
    console.print(f"LLM 规划器：{flag}")
    console.print(f"  base_url = {st['base_url']}")
    console.print(f"  model    = {st['model']}")
    console.print(f"  api_key  = {st['api_key']}   [dim]（仅显示前缀，不泄露完整密钥）[/dim]")

    try:
        agent, _ = build_agent(args)
    except K8sUnavailable as exc:
        console.print(f"[red]集群不可达：{exc}[/red]")
        return 2
    ok, info = agent.cluster_status()
    console.print(f"集群连接：{'✅' if ok else '❌'} {info}")

    import json

    desc = agent.describe_self()
    console.print(
        Panel(
            json.dumps(desc["policy"], indent=2, ensure_ascii=False)[:2600],
            title="当前生效的安全策略",
            border_style="blue",
        )
    )
    return 0


def cmd_diagnose(args) -> int:
    ns, name = (
        args.target.split("/", 1) if "/" in args.target else ("demo", args.target)
    )
    return run_diagnosis(args, ns, name, args.kind, use_llm=args.llm)


def cmd_ask(args) -> int:
    """自然语言入口：先解析意图，再走与 diagnose 完全相同的链路。"""
    from .intent import IntentParser, IntentUnresolved

    try:
        agent, _ = build_agent(args)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]集群不可达：{exc}[/red]")
        return 2

    parser = IntentParser(agent.k8s, agent.policy)
    try:
        intent = parser.parse(args.text, args.namespace or "")
    except IntentUnresolved as exc:
        console.print(Panel(f"[yellow]{exc}[/yellow]", title="🤔 无法确定你要诊断什么",
                            border_style="yellow"))
        return 2

    console.print(
        f"🧭 [bold]{intent.interpretation}[/bold]  "
        f"[dim](来源={intent.source}，置信度={intent.confidence})[/dim]"
    )
    # 意图解析只产出结构化参数；后续链路与 diagnose 完全相同（含所有安全门禁）
    return run_diagnosis(
        args, intent.namespace, intent.workload, intent.kind,
        use_llm=(intent.planner == "llm" or args.llm), turns=intent.turns,
    )


def run_diagnosis(
    args, ns: str, name: str, kind: str, *, use_llm: bool = False, turns: int = 1
) -> int:
    """诊断主流程（diagnose 与 ask 共用，避免两条路径行为漂移）。"""
    agent, audit = build_agent(args)
    planner = LLMPlanner(agent.k8s) if use_llm else RuleBasedPlanner(agent.k8s)
    trace = new_trace_id()

    audit.log_intent(trace, args.operator, f"诊断 {kind}/{name} @ {ns}")
    if turns > 1:
        from .loop import AgentLoop

        run = AgentLoop(planner, agent, max_turns=turns).run(ns, name, kind)
        diag = run.diagnosis
        if run.turns:
            console.print(
                f"[dim]多轮取证：共 {run.rounds} 轮、{len(run.turns)} 次只读动作"
                f"（{run.stopped_reason}）[/dim]"
            )
        assert diag is not None
    else:
        diag = planner.diagnose(ns, name, kind)

    # --- 知识沉淀：先查历史同类故障 ---
    from .knowledge import KnowledgeStore, extract_keywords

    kb = KnowledgeStore(args.knowledge)
    evidence_text = " ".join(f"{e.ref} {e.detail}" for e in diag.evidence)
    kw = extract_keywords(evidence_text)
    hint = kb.hint_for(diag.matched_signature, kw, name)
    if hint:
        diag.findings.append(f"📚 {hint}")

    audit.log_diagnosis(
        trace, args.operator, diag.conclusion, [e.to_dict() for e in diag.evidence]
    )
    render_diagnosis(diag)

    if not args.execute or not diag.candidates:
        return 0

    pick = args.pick
    if pick is None:
        from rich.prompt import IntPrompt

        pick = IntPrompt.ask(
            "选择要执行的候选动作编号（0 = 不执行）", default=0
        )
    if not pick:
        console.print("[dim]已取消，未执行任何动作。[/dim]")
        return 0

    cand = diag.candidates[pick - 1]
    prop = agent.propose(
        cand.tool, cand.params, rationale=cand.rationale, evidence=diag.evidence, trace_id=trace
    )
    confirmer = CLIConfirmer(args.operator)
    decision = confirmer.confirm(prop)
    audit.log_decision(trace, decision)
    res = agent.execute(prop, decision, trace_id=trace)
    console.print(f"执行结果：[bold]{res.status}[/bold] {res.output or res.error} ({res.duration_ms}ms)")

    # --- 知识沉淀：把本次「症状→根因→处置→结果」落库 ---
    kb.record_from_diagnosis(
        namespace=ns,
        workload=name,
        kind=kind,
        signature=diag.matched_signature,
        conclusion=diag.conclusion,
        evidence_text=evidence_text,
        treatment=f"{cand.tool} {cand.params}",
        treatment_tier=prop.effective_tier.value,
        outcome=res.status,
        outcome_note=res.output or res.error,
        operator=args.operator,
    )
    return 0


def cmd_refuse(args) -> int:
    agent, _ = build_agent(args)
    render_refusal(agent.refuse(args.request, args.rule))
    return 0


def cmd_demo(args) -> int:
    agent, audit = build_agent(args)
    from .demo import run_demo

    return run_demo(agent, audit, auto=args.auto)


def cmd_itbench(args) -> int:
    """用 ITBench-Lite 的真实 K8s 事故数据评测诊断能力（非自证）。"""
    from .itbench import REVIEWED_EXPECTATIONS, load_scenario, run_scenario, summarise_results

    base = Path(args.dir)
    scenes = args.scenarios.split(",") if args.scenarios else sorted(
        f.stem for f in (base / "gt").glob("*.yaml")
    )
    if not scenes:
        console.print(f"[red]在 {base}/gt 下没找到真值文件。[/red]")
        return 2

    console.print(f"载入 {len(scenes)} 个 ITBench 场景…")
    results = []
    for sid in scenes:
        with console.status(f"评测 {sid}…"):
            try:
                results.append(run_scenario(base, sid))
            except Exception as exc:  # noqa: BLE001
                from .itbench import ScenarioResult

                r = ScenarioResult(scn_id=sid)
                r.error = str(exc)
                results.append(r)

    t = Table(title="ITBench 真实数据评测", show_header=True, header_style="bold")
    t.add_column("场景", style="cyan", width=14)
    t.add_column("判定", width=14)
    t.add_column("工作负载", width=22)
    t.add_column("分类（期望→实际）", width=38)
    style = {"hit": "green", "partial": "yellow", "miss": "red",
             "undetectable": "dim", "skip": "dim", "error": "red"}
    for r in results:
        t.add_row(
            r.scn_id,
            f"[{style[r.verdict]}]{r.verdict}[/{style[r.verdict]}]",
            r.workload or "-",
            f"{r.signature_expected or '-'} → {r.signature_actual or '-'}",
        )
    console.print(t)

    counts = summarise_results(results)
    console.print()
    console.print(f"  ✓ 命中        [green]{counts.get('hit', 0)}[/green]")
    console.print(f"  ◐ 部分正确    [yellow]{counts.get('partial', 0)}[/yellow]")
    console.print(f"  ✗ 未命中      [red]{counts.get('miss', 0)}[/red]")
    console.print(f"  · 设计上看不到 {counts.get('undetectable', 0)}")
    console.print(f"  · 超出能力范围 {counts.get('skip', 0)}")

    graded = [r for r in results if r.verdict in ("hit", "partial", "miss")]
    if graded:
        hit = counts.get("hit", 0)
        console.print(
            f"\n  可评分场景 {len(graded)} 个，完全命中 **{hit}/{len(graded)}**"
            f"（{hit/len(graded):.0%}）"
        )
    console.print(
        "\n[dim]注：本评测用真值**指定工作负载**，只评「诊断与处置」这一半；"
        "不评「从全集群遥测定位根因实体」——那是本项目尚未具备的能力。[/dim]"
    )

    for r in results:
        if r.verdict in ("miss", "partial") and r.note:
            console.print(f"\n[yellow]{r.scn_id}[/yellow] {r.note}")
            if r.conclusion:
                console.print(f"  实际结论：{r.conclusion[:180]}")
    return 0


def cmd_fuzz(args) -> int:
    """用大模型生成规则之外的故障场景，反向测试规则引擎。"""
    from .fuzz import (
        FuzzGenerator,
        FuzzUnavailable,
        analyse_generated,
        save_generated,
        summarise,
        to_markdown,
    )

    gen = FuzzGenerator()
    if not gen.available:
        console.print(
            "[red]未配置模型凭据，无法生成场景。[/red]\n"
            "[dim]在 .env 里设置 DEEPSEEK_API_KEY，或使用 --analyze 只分析已有文件。[/dim]"
        )
        return 2

    if args.analyze:
        import yaml as _yaml

        raw = _yaml.safe_load(Path(args.analyze).read_text(encoding="utf-8")) or []
        cases = raw if isinstance(raw, list) else raw.get("cases", [])
        console.print(f"从 {args.analyze} 载入 {len(cases)} 个场景，开始分析…")
    else:
        with console.status(f"让模型设计 {args.n} 个规则之外的故障场景…"):
            try:
                result = gen.generate(args.n)
            except FuzzUnavailable as exc:
                console.print(f"[red]{exc}[/red]")
                return 2
        cases = result.cases
        if not cases:
            console.print("[yellow]模型没有产出可用场景。[/yellow]")
            return 1
        path = save_generated(cases, args.name)
        console.print(
            f"✅ 生成 {len(cases)} 个场景 → [cyan]{path}[/cyan]"
            f"  [dim](模型={result.model}，tokens={result.usage.get('total_tokens', '?')})[/dim]"
        )
        console.print("[dim]注：这些用例的期望值是模型假设，需人工复核后才进主用例集。[/dim]")

    spots = analyse_generated(cases)
    counts = summarise(spots)

    console.print()
    t = Table(title="规则引擎在新场景上的盲区", show_header=True, header_style="bold")
    t.add_column("用例", style="cyan", width=24)
    t.add_column("判定", width=22)
    t.add_column("标题")
    for sp in spots:
        style = {"false_positive": "red", "miss": "yellow",
                 "unrecognized": "dim", "ok": "green"}[sp.verdict]
        t.add_row(sp.case_id, f"[{style}]{sp.verdict}[/{style}]", sp.title[:44])
    console.print(t)

    console.print()
    console.print(f"  🚨 假阳性提议  [red]{counts.get('false_positive', 0)}[/red]")
    console.print(f"  ⚠️ 漏报        [yellow]{counts.get('miss', 0)}[/yellow]")
    console.print(f"  ·  特征未识别  {counts.get('unrecognized', 0)}")
    console.print(f"  ✓  通过        [green]{counts.get('ok', 0)}[/green]")

    if args.md:
        Path(args.md).write_text(
            to_markdown(spots, f"规则引擎盲区报告（{len(cases)} 个生成场景）"),
            encoding="utf-8",
        )
        console.print(f"\n[dim]Markdown 报告已写入 {args.md}[/dim]")

    interesting = [s for s in spots if s.verdict in ("false_positive", "miss")]
    if interesting:
        console.print("\n[bold]值得人工复核的线索：[/bold]")
        for sp in interesting:
            console.print(sp.render())
            console.print()
    return 0


def cmd_web(args) -> int:
    from .web import serve

    serve(
        host=args.host,
        port=args.port,
        operator=args.operator,
        planner=args.planner,
        kubeconfig=args.kubeconfig,
        audit_path=args.audit,
        knowledge_path=args.knowledge,
        policy_path=args.policy,
        demo=args.demo,
        cooldown=args.cooldown,
    )
    return 0


def cmd_knowledge(args) -> int:
    from .knowledge import KnowledgeStore

    kb = KnowledgeStore(args.knowledge)
    if args.hint:
        hint = kb.hint_for(args.hint, [], args.workload or "")
        console.print(hint or "[dim]没有匹配的历史记录。[/dim]")
        return 0
    if args.stats or not args.search:
        import json as _json

        console.print(
            Panel(
                _json.dumps(kb.stats(), ensure_ascii=False, indent=2),
                title="知识库统计",
                border_style="blue",
            )
        )
    if args.search:
        hits = kb.similar(args.search, [], args.workload or "", limit=args.limit)
        if not hits:
            console.print("[dim]没有匹配的历史记录。[/dim]")
            return 0
        for score, e in hits:
            console.print(f"[cyan]score={score:.3f}[/cyan]  {e.render()}")
        return 0
    if not args.search and not args.stats:
        entries = kb.all()[-args.limit :]
        for e in entries:
            console.print(e.render())
    return 0


def cmd_eval(args) -> int:
    from .evals import DEFAULT_CASES_DIR, load_cases, record_case, run_all

    # --- 采集模式：把真实集群的当前状态固化成一条评测用例 ---
    if args.record:
        agent, _ = build_agent(args)
        ns, name = args.record.split("/", 1) if "/" in args.record else ("demo", args.record)
        out = record_case(
            agent.k8s,
            ns,
            name,
            args.kind,
            args.id or f"recorded-{name}",
            args.out or (ROOT / "evals" / "recorded" / f"{args.id or name}.yaml"),
            expect_signature=args.expect_signature or "unknown",
            acceptable_actions=(args.acceptable or "").split(",") if args.acceptable else [],
            title=args.title or f"{args.kind}/{name} 现场采集",
        )
        console.print(f"✅ 已采集用例：{out}")
        console.print("[dim]请人工核对并修正 expect.signature 后，再纳入评测集。[/dim]")
        return 0

    # --- 回放模式 ---
    cases = load_cases(args.cases or DEFAULT_CASES_DIR)
    if not cases:
        console.print("[red]未找到任何用例。[/red]")
        return 2

    # 规划器选择：规则引擎（默认，零成本）或 LLM（需 API Key）
    planner_factory = None
    if args.planner == "llm":
        from .planner import LLMPlanner

        # 轻量可用性探测，避免 37 个用例逐个失败
        import os as _os

        if not (_os.environ.get("OMAGENT_LLM_API_KEY") or _os.environ.get("DEEPSEEK_API_KEY")):
            console.print("[red]选择了 LLM 规划器但未配置 API Key。[/red]")
            return 2
        planner_factory = LLMPlanner
        console.print("[cyan]使用 LLM 规划器（会产生 API 调用成本）[/cyan]")

    def _progress(i: int, total: int, r) -> None:
        mark = "[green]✓[/green]" if r.ok else (
            "[yellow]缺口[/yellow]" if r.known_gap else "[red]✗[/red]"
        )
        console.print(f"  [{i:>2}/{total}] {mark} {r.case_id}", highlight=False)

    if args.turns > 1:
        console.print(
            f"[cyan]启用多轮取证循环（最多 {args.turns} 轮）："
            f"规划器可先要求查证据，Agent 真正执行后再下结论[/cyan]"
        )

    report = run_all(
        cases,
        planner_factory=planner_factory,
        on_progress=_progress,
        use_loop=args.turns > 1,
        max_turns=args.turns,
    )

    t = Table(title="评测结果", show_header=True, header_style="bold")
    t.add_column("用例", style="cyan", width=10)
    t.add_column("难度", width=7)
    t.add_column("标题")
    t.add_column("判定", width=10)
    for r in report.results:
        if r.ok:
            verdict = "[green]通过[/green]"
        elif r.known_gap:
            verdict = "[yellow]已知缺口[/yellow]"
        else:
            verdict = "[red]失败[/red]"
        t.add_row(r.case_id, r.difficulty, r.title[:46], verdict)
    console.print(t)

    console.print()
    console.print(f"用例总数            {report.total}")
    console.print(f"严格通过            {report.passed}/{report.total}  ({report.pass_rate:.1%})")
    console.print(
        f"已知能力缺口        {report.known_gap_count} 条"
        f"（预期失败 {report.known_gap_failures} 条，不计入能力达标率）"
    )
    console.print(f"[bold]能力达标率          {report.capability_pass_rate:.1%}[/bold]")
    console.print(f"诊断特征准确率      {report.signature_accuracy:.1%}")
    sc = report.strategy_counts
    console.print(
        f"策略分布            "
        f"提出修复 [cyan]{sc.get('remediate', 0)}[/cyan] ／ "
        f"要求取证 [cyan]{sc.get('investigate', 0)}[/cyan] ／ "
        f"明确不介入 [cyan]{sc.get('abstain', 0)}[/cyan]"
    )
    if report.remediation_expected_cases:
        console.print(
            f"预期修复用例命中    {report.remediation_hit}/{report.remediation_expected_cases}"
            f"（延后 {report.deferred_cases} 条——**不等于失败**，见立项材料 6.4）"
        )
    danger_style = "red" if report.dangerous_proposal_count else "green"
    leak_style = "red" if report.gate_leak_count else "green"
    console.print(
        f"危险提议数          [{danger_style}]{report.dangerous_proposal_count}[/]"
        f"  门禁泄漏数 [{leak_style}]{report.gate_leak_count}[/]"
    )

    failures = [r for r in report.results if not r.ok and not r.known_gap]
    if failures:
        console.print("\n[red]失败用例：[/red]")
        for r in failures:
            console.print(f"  [{r.case_id}] {r.title}")
            for f in r.failures:
                console.print(f"      ✗ {f}")

    gaps = [r for r in report.results if r.known_gap and not r.ok]
    if gaps:
        console.print("\n[yellow]已知能力缺口（当前实现的盲区）：[/yellow]")
        for r in gaps:
            console.print(f"  [{r.case_id}] {r.title}")
            console.print(f"      期望 {r.signature_expected} ／ 实际 {r.signature_actual}；"
                          f"提议 {r.proposed_actions}")

    if args.json:
        import json

        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"\n[dim]JSON 报告已写入 {args.json}[/dim]")

    # 退出码：能力范围内的失败或任何安全问题都算失败
    return 0 if (not failures and not report.dangerous_proposal_count and not report.gate_leak_count) else 1


def cmd_audit(args) -> int:
    audit = AuditLog(args.audit)
    if args.verify:
        ok, msg = audit.verify()
        console.print(f"{'✅' if ok else '❌'} 审计链校验：{msg}")
        return 0 if ok else 1
    recs = audit.records()
    t = Table(title=f"审计日志（{len(recs)} 条）", show_header=True, header_style="bold")
    t.add_column("seq", width=5)
    t.add_column("时间", width=20)
    t.add_column("事件", width=14)
    t.add_column("摘要")
    for r in recs[-args.limit :]:
        p = r["payload"]
        summary = p.get("conclusion") or p.get("output") or p.get("tool") or p.get("request") or ""
        if r["event"] == "execution":
            summary = f"{p.get('status')} {summary}"
        t.add_row(str(r["seq"]), r["ts"][:19], r["event"], str(summary)[:90])
    console.print(t)
    return 0


def main(argv: list[str] | None = None) -> int:
    # 自动装载工作区 .env（只装载 OMAGENT_/DEEPSEEK_/OPENAI_ 前缀，从不打印值）
    from .config import ensure_local_bypass, load_env_file

    load_env_file()
    # 代理只用于外部 LLM API；本地/集群内地址必须绕过，
    # 否则 kubernetes 客户端连 127.0.0.1:<port> 也会走代理而失败。
    ensure_local_bypass()

    p = argparse.ArgumentParser(prog="omagent", description="Kubernetes 运维 Agent（L1 档位）")
    p.add_argument("--kubeconfig", default=None)
    p.add_argument("--policy", default=None)
    p.add_argument("--audit", default=str(ROOT / "var" / "audit.jsonl"))
    p.add_argument("--knowledge", default=str(ROOT / "var" / "knowledge.jsonl"))
    p.add_argument("--operator", default="sre@example.com")
    p.add_argument("--read-only", action="store_true", help="只读模式，拒绝一切写操作")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="显示集群连接与生效策略")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("ask", help="自然语言入口：用一句话描述问题")
    s.add_argument("text", help="例如：api-gateway 一直重启，帮我看看")
    s.add_argument("--namespace", default=None, help="限定命名空间")
    s.add_argument("--llm", action="store_true", help="用 LLM 规划器诊断")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("diagnose", help="诊断一个工作负载")
    s.add_argument("target", help="<ns>/<workload> 或 <workload>")
    s.add_argument("--kind", default="deployment")
    s.add_argument("--execute", action="store_true", help="诊断后允许选择候选动作执行")
    s.add_argument("--pick", type=int, default=None, help="直接选择候选编号，跳过交互")
    s.add_argument("--llm", action="store_true", help="使用 LLM 规划器（需配置 API Key）")
    s.set_defaults(fn=cmd_diagnose)

    s = sub.add_parser("refuse", help="演示：请求一个越界动作，观察拒绝")
    s.add_argument("request")
    s.add_argument("--rule", default="delete_namespace")
    s.set_defaults(fn=cmd_refuse)

    s = sub.add_parser("audit", help="查看或校验审计日志")
    s.add_argument("--verify", action="store_true")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("demo", help="运行三个演示剧本（PRD 第 9 节）")
    s.add_argument("--auto", action="store_true", default=True, help="自动确认（演示用）")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("itbench", help="用 ITBench-Lite 真实事故数据评测（非自证）")
    s.add_argument("--dir", default=str(ROOT / "var" / "itbench"), help="ITBench 数据目录")
    s.add_argument("--scenarios", default=None, help="逗号分隔的场景名；默认全部")
    s.set_defaults(fn=cmd_itbench)

    s = sub.add_parser("fuzz", help="用大模型生成规则之外的场景，反测规则引擎")
    s.add_argument("--n", type=int, default=6, help="生成多少个场景")
    s.add_argument("--name", default="round1", help="输出文件名（evals/generated/<name>.yaml）")
    s.add_argument("--analyze", default=None, help="只分析已有的生成文件，不再调用模型")
    s.add_argument("--md", default=None, help="把盲区报告导出为 Markdown")
    s.set_defaults(fn=cmd_fuzz)

    s = sub.add_parser("web", help="启动 Web 审批界面（仅监听本机）")
    s.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument(
        "--planner", choices=["rule", "llm"], default="rule",
        help="默认规划器（界面上可切换）",
    )
    s.add_argument("--demo", action="store_true",
                   help="开启演示模式：界面上可直接注入沙箱故障，并缩短变更冷却期")
    s.add_argument("--cooldown", type=int, default=None,
                   help="同一工作负载的变更冷却秒数（默认 300；演示模式自动降到 20）")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("knowledge", help="查看/检索沉淀的故障处置知识")
    s.add_argument("--search", default=None, help="按故障特征签名检索，如 oom_killed")
    s.add_argument("--hint", default=None, help="按签名生成一句历史提示")
    s.add_argument("--workload", default=None)
    s.add_argument("--stats", action="store_true", help="只看统计")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_knowledge)

    s = sub.add_parser("eval", help="运行评测集回放（PRD 13.2）")
    s.add_argument("--cases", default=None, help="用例目录，默认 evals/cases")
    s.add_argument(
        "--planner", choices=["rule", "llm"], default="rule",
        help="规划器：rule=确定性规则引擎（默认，零成本）；llm=大模型",
    )
    s.add_argument(
        "--turns", type=int, default=1,
        help="多轮取证轮次：1=单轮（默认）；>1 启用 AgentLoop，规划器先取证再下结论",
    )
    s.add_argument("--json", default=None, help="把 JSON 报告写入指定路径")
    s.add_argument("--record", default=None, help="采集模式：<ns>/<workload>，把现场状态固化成用例")
    s.add_argument("--record-kind", dest="kind", default="deployment")
    s.add_argument("--id", default=None, help="采集用例的 id")
    s.add_argument("--out", default=None, help="采集用例的输出路径")
    s.add_argument("--title", default=None)
    s.add_argument("--expect-signature", dest="expect_signature", default=None)
    s.add_argument("--acceptable", default=None, help="可接受动作，逗号分隔")
    s.set_defaults(fn=cmd_eval)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
